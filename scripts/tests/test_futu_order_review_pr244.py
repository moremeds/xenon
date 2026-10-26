"""Astra PR #244 review replays (F1-F4) as committed regressions.

Real routes, Gate 4, the Futu order worker and the autospec-bound fake Futu
context; only the IB quote fetch is stubbed (`quotes`). Ids, quantities and
prices are made-up test values, not market data.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from scripts.tests import test_futu_order_quote_rules as _rules
from scripts.tests.test_futu_observation import _env  # noqa: F401 — autouse fixture
from scripts.tests.test_futu_order_intent import EXT_OID, LIVE, TRAIL_LIMIT, _import_external, _observe
from scripts.tests.test_futu_order_quote_rules import MODIFY_TYPES, _modify, _place
from scripts.tests.test_orders_futu_branch import (  # noqa: F401 — autouse fixture
    LIVE_ACC,
    _configured,
    _modifies,
    _post,
    _row,
)
from xenon.api.routes.orders import orders_payload_for_scope
from xenon.api.services import futu_observation as fo
from xenon.execution import orders_store

pytestmark = pytest.mark.committed_db

futu_ib_quotes = _rules.futu_ib_quotes
quotes = _rules.quotes

RATIO_LIMIT = {**TRAIL_LIMIT, "trailType": "RATIO", "trailValue": 1.5}  # AMOUNT 2.5 in TRAIL_LIMIT


@pytest.fixture(autouse=True)
def _modify_on(monkeypatch):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", MODIFY_TYPES)


def _shown(oid):
    [row] = [o for o in orders_payload_for_scope(LIVE)["open_orders"] if o["brokerOrderId"] == oid]
    return row


def _untouched(quotes, sid, oid, before):
    """Nothing sent; row, reservation and intent as before; cancel still works."""
    assert _modifies(quotes) == []
    after = _row(sid)
    assert (after.state, after.quantity) == (before.state, before.quantity)
    assert after.legs.get("pending_amend_id") is None and after.legs.get("pending_amend_qty") is None
    assert after.legs.get("futu_intent") == before.legs.get("futu_intent")
    assert after.legs.get("futu_terms") == before.legs.get("futu_terms")
    cancel = _post(quotes, "/orders/cancel", {"brokerOrderId": oid}, env="live")
    assert cancel.status_code == 200 and cancel.json()["confirmed"] is True, cancel.text


# -- F1: the broker's trail unit must equal the frozen intent unit -------------


@pytest.mark.parametrize(
    "fields, broker_unit",
    [(RATIO_LIMIT, "AMOUNT"), (TRAIL_LIMIT, "RATIO"), (RATIO_LIMIT, "N/A"), (TRAIL_LIMIT, None)],
)
def test_f1_a_quantity_amend_never_changes_or_guesses_the_trail_unit(quotes, fields, broker_unit):
    sid, oid = _place(quotes, fields)
    if broker_unit is None:
        quotes.ctx.book[oid].pop("trail_type")
    else:
        quotes.ctx.book[oid]["trail_type"] = broker_unit  # changed (or unreadable) at Futu
    before = _row(sid)

    mod = _modify(quotes, oid, newQuantity=3)

    assert mod.status_code == 400 and mod.json()["reason_code"] == "FUTU_ORDER_NOT_MODIFIABLE", mod.text
    _untouched(quotes, sid, oid, before)
    assert _row(sid).legs["futu_intent"]["trail_type"] == fields["trailType"]  # never copied from the read


# -- F2: an external order's session must be established -----------------------


@pytest.mark.parametrize("flag", [True, "absent", "N/A"])
def test_f2_an_external_order_with_an_unknown_session_is_not_modifiable(quotes, flag):
    cols = {"order_type": "NORMAL", "price": 500.2, "session": "N/A"}
    if flag != "absent":
        cols["fill_outside_rth"] = flag
    sid = _import_external(quotes, cols)
    before = _row(sid)

    mod = _modify(quotes, EXT_OID, newQuantity=2)

    assert mod.status_code == 400 and mod.json()["reason_code"] == "FUTU_ORDER_NOT_MODIFIABLE", mod.text
    _untouched(quotes, sid, EXT_OID, before)


def test_f2_an_external_na_session_order_that_cannot_fill_outside_rth_is_rth(quotes):
    _import_external(quotes, {"order_type": "NORMAL", "price": 500.2, "session": "N/A", "fill_outside_rth": False})

    mod = _modify(quotes, EXT_OID, newQuantity=2)

    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    [kw] = _modifies(quotes)
    assert kw["qty"] == 2 and "session" not in kw


@pytest.mark.parametrize("session", ["ETH", "OVERNIGHT"])
def test_f2_an_external_reported_session_outside_the_allowlist_is_refused(quotes, monkeypatch, session):
    monkeypatch.setenv("XENON_FUTU_TUPLES_LIVE", "STK:LMT:DAY:RTH")
    sid = _import_external(quotes, {"order_type": "NORMAL", "price": 500.2, "session": session})
    before = _row(sid)

    mod = _modify(quotes, EXT_OID, newQuantity=2)

    assert mod.status_code == 400 and mod.json()["reason_code"] == "FUTU_SESSION_UNSUPPORTED", mod.text
    _untouched(quotes, sid, EXT_OID, before)


# -- F3: confirmation needs the same order type and trail unit -----------------


@pytest.mark.parametrize(
    "after_send",
    [{"trail_type": "AMOUNT"}, {"trail_type": "N/A"}, {"order_type": "TRAILING_STOP"}],
)
def test_f3_a_post_send_read_with_another_type_or_unit_is_not_confirmation(quotes, monkeypatch, after_send):
    sid, oid = _place(quotes, RATIO_LIMIT)
    before = _row(sid)
    real = quotes.ctx.modify_order

    def _reported_otherwise(**kw):
        out = real(**kw)
        quotes.ctx.book[oid].update(after_send)
        return out

    monkeypatch.setattr(quotes.ctx, "modify_order", _reported_otherwise)

    mod = _modify(quotes, oid, newTrailValue=2)

    assert mod.status_code == 200 and mod.json()["confirmed"] is False, mod.text
    [kw] = _modifies(quotes)
    assert (kw["trail_type"], kw["trail_value"]) == ("RATIO", 2.0)
    row = _row(sid)
    assert row.legs["pending_amend_id"] is not None and row.legs["pending_amend_qty"] is not None
    assert row.legs["futu_terms"] == before.legs["futu_terms"]  # nothing confirmed
    again = _modify(quotes, oid, seq=2, newQuantity=3)  # the read still contradicts the placement
    assert again.status_code == 400 and again.json()["reason_code"] == "FUTU_ORDER_NOT_MODIFIABLE", again.text
    quotes.ctx.book[oid].update(trail_type="RATIO", order_type="TRAILING_STOP_LIMIT")  # Futu reports it right again
    again = _modify(quotes, oid, seq=3, newQuantity=3)
    assert again.status_code == 409 and again.json()["reason_code"] == "MODIFY_PENDING", again.text
    assert len(_modifies(quotes)) == 1


# -- F4: an older observation never overwrites newer terms ---------------------


def test_f4_a_delayed_observation_does_not_revert_confirmed_trail_terms(quotes):
    sid, oid = _place(quotes, TRAIL_LIMIT)
    read_at = datetime.now(timezone.utc)
    stale = fo.observation_from_row(dict(quotes.ctx.book[oid]), LIVE, read_at)  # read starts at 2.5
    time.sleep(0.01)

    mod = _modify(quotes, oid, newTrailValue=3)
    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    confirmed = _row(sid).legs["futu_order"]

    orders_store.mirror_futu_order(stale, broker_account=LIVE_ACC, account_env="live", observed_at=read_at)

    kept = _row(sid).legs["futu_order"]
    assert {k: kept[k] for k in ("trail_type", "trail_value", "observed_at")} == {
        k: confirmed[k] for k in ("trail_type", "trail_value", "observed_at")
    }  # terms, unit and their read time stay together

    assert _row(sid).legs["futu_terms"]["trail_value"] == "3"
    assert _row(sid).legs["futu_order"]["trail_value"] != "2.5"
    assert _shown(oid)["trailValue"] == 3.0


def test_f4_a_newer_external_edit_still_shows(quotes):
    sid, oid = _place(quotes, TRAIL_LIMIT)
    mod = _modify(quotes, oid, newTrailValue=3)
    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    time.sleep(0.01)
    quotes.ctx.book[oid]["trail_value"] = 3.5  # edited in the Futu app after the amend

    assert _observe(LIVE, quotes.ctx)["complete"]

    assert _shown(oid)["trailValue"] == 3.5
    assert _row(sid).legs["futu_terms"]["trail_value"] == "3"  # confirmed terms unchanged by observation


# -- Round 2: observed unit and terms come from one read, ordered by read time --

EXT_TRAIL_LIMIT = {
    "order_type": "TRAILING_STOP_LIMIT",
    "price": 3.0,
    "trail_type": "AMOUNT",
    "trail_value": 2.5,
    "trail_spread": 0.05,
    "session": "RTH",
}


@pytest.mark.parametrize("mirror_fails", [False, True])
def test_r2_1_an_external_unit_edit_shows_the_unit_of_the_confirming_read(quotes, monkeypatch, mirror_fails):
    if mirror_fails:  # settlement itself publishes the confirming read
        from xenon.api.services import order_entry

        monkeypatch.setattr(order_entry, "_futu_mirror", lambda *a, **kw: None)
    sid = _import_external(quotes, EXT_TRAIL_LIMIT)
    assert _shown(EXT_OID)["trailType"] == "AMOUNT"
    quotes.ctx.book[EXT_OID].update(trail_type="RATIO", trail_value=1.5)  # Futu app edit, not observed yet

    mod = _modify(quotes, EXT_OID, newTrailValue=2)

    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    [kw] = _modifies(quotes)
    assert (kw["trail_type"], kw["trail_value"]) == ("RATIO", 2.0)
    shown = _shown(EXT_OID)
    assert (shown["trailType"], shown["trailValue"]) == ("RATIO", 2.0)
    assert "futu_intent" not in _row(sid).legs  # the unit came from the read, not a made-up intent


def test_r2_1_a_restored_own_unit_shows_with_the_confirmed_value(quotes):
    sid, oid = _place(quotes, RATIO_LIMIT)
    quotes.ctx.book[oid]["trail_type"] = "AMOUNT"  # Futu app edit, observed
    assert _observe(LIVE, quotes.ctx)["complete"]
    assert _shown(oid)["trailType"] == "AMOUNT"
    quotes.ctx.book[oid]["trail_type"] = "RATIO"  # back to the placement unit, not observed yet
    intent = _row(sid).legs["futu_intent"]

    mod = _modify(quotes, oid, newTrailValue=2)

    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    [kw] = _modifies(quotes)
    assert (kw["trail_type"], kw["trail_value"]) == ("RATIO", 2.0)
    shown = _shown(oid)
    assert (shown["trailType"], shown["trailValue"]) == ("RATIO", 2.0)
    assert _row(sid).legs["futu_intent"] == intent


@pytest.mark.parametrize("lands", ["before", "after"])
def test_r2_2_an_edit_observed_between_confirmation_and_settlement_stays(quotes, monkeypatch, lands):
    """The edit is read after the confirming read; its mirror commits before
    the settle write ("before") or after it ("after")."""
    from xenon.execution import gate4

    sid, oid = _place(quotes, TRAIL_LIMIT)
    real = gate4.settle_modify
    seen = {}

    def _edit_then_settle(submission_id, amend_id, outcome, *a, **kw):
        if outcome != "confirmed" or seen:
            return real(submission_id, amend_id, outcome, *a, **kw)
        time.sleep(0.01)
        quotes.ctx.book[oid]["trail_value"] = 3.5  # Futu app edit after the confirming read
        read_at = datetime.now(timezone.utc)
        newer = fo.observation_from_row(dict(quotes.ctx.book[oid]), LIVE, read_at)

        def _mirror():
            orders_store.mirror_futu_order(newer, broker_account=LIVE_ACC, account_env="live", observed_at=read_at)

        if lands == "before":
            _mirror()
            legs = _row(sid).legs
            seen.update(observed=legs["futu_order"]["trail_value"], pending=legs.get("pending_amend_id"))
            assert seen == {"observed": "3.5", "pending": amend_id}
        out = real(submission_id, amend_id, outcome, *a, **kw)
        if lands == "after":
            _mirror()
        seen["ran"] = True
        return out

    monkeypatch.setattr(gate4, "settle_modify", _edit_then_settle)

    mod = _modify(quotes, oid, newTrailValue=3)

    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    assert seen  # the hook ran at the settle boundary
    [kw] = _modifies(quotes)
    assert (kw["trail_type"], kw["trail_value"]) == ("AMOUNT", 3.0)
    legs = _row(sid).legs
    assert legs["futu_terms"]["trail_value"] == "3"  # the confirmed amend is still recorded
    assert legs.get("pending_amend_id") is None  # and its generation settled
    assert _shown(oid)["trailValue"] == 3.5
