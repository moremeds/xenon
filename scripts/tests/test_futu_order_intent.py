"""X5a task 2: placement intent, NULL price, idempotency and binding (plan v4 § 3).

Every order goes through the real FastAPI routes, Gate 4 reservation, the
Futu order worker and the observation mirror over an autospec-bound fake
Futu trade context (`FakeOrderCtx`); only the IB quote gate is stubbed
(`futu` fixture) unless a test says otherwise. All ids, quantities and
prices are made-up test values, not market data.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from scripts.tests.test_futu_observation import (  # noqa: F401 — autouse fixture
    ACC,
    SCOPE,
    FakeTradeCtx,
    _acc_row,
    _env,
    _order,
    resolver,
)
from scripts.tests import test_orders_futu_branch as _branch
from scripts.tests.test_orders_futu_branch import (  # noqa: F401 — autouse fixture
    LIVE_ACC,
    _configured,
    _modifies,
    _placed,
    _post,
    _row,
    _stock,
)
from xenon.api.routes.orders import orders_payload_for_scope
from xenon.api.services import futu_observation as fo
from xenon.api.services import order_entry
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions
from xenon.execution import futu_orders, gate4, orders_store
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.committed_db

# The route fixtures of test_orders_futu_branch (fake Futu context + worker).
futu = _branch.futu
futu_ib_quotes = _branch.futu_ib_quotes

LIVE = AccountScope(broker="FUTU", account_env="live", broker_account=LIVE_ACC)
LIVE_TUPLES = ",".join(
    f"STK:{t}:DAY:{s}"
    for t, s in [
        ("LMT", "RTH"),
        ("LMT", "ETH"),
        ("LMT", "OVERNIGHT"),
        ("STP", "RTH"),
        ("STP LMT", "RTH"),
        ("MIT", "RTH"),
        ("LIT", "RTH"),
        ("TRAIL", "RTH"),
        ("TRAIL LIMIT", "RTH"),
    ]
)
STP = {"orderType": "STP", "auxPrice": 510.5}
TRAIL = {"orderType": "TRAIL", "trailType": "RATIO", "trailValue": 1.5}
TRAIL_LIMIT = {"orderType": "TRAIL LIMIT", "trailType": "AMOUNT", "trailValue": 2.5, "trailSpread": 0.05}


@pytest.fixture
def live_types(monkeypatch):
    monkeypatch.setenv("XENON_FUTU_TUPLES_LIVE", LIVE_TUPLES)


def _typed(cid, **fields):
    """A stock place body; `limitPrice` only when given."""
    body = _stock(cid=cid)
    body.pop("limitPrice")
    return {**body, **{k: v for k, v in fields.items() if v is not None}}


def _sent(futu):
    return [kw for name, kw in futu.ctx.mutations if name == "place_order"]


def _rows_for(oid):
    with get_sync_engine().connect() as conn:
        return conn.execute(
            select(func.count()).select_from(order_submissions).where(order_submissions.c.broker_order_id == oid)
        ).scalar()


def _observe(scope, ctx):
    return fo.run_pass(scope, ctx, resolve_meta=resolver(), audit=False)


def _set_legs(sid, **changes):
    row = _row(sid)
    legs = dict(row.legs)
    for k, v in changes.items():
        if v is None:
            legs.pop(k, None)
        else:
            legs[k] = v
    with get_sync_engine().begin() as conn:
        conn.execute(update(order_submissions).where(order_submissions.c.submission_id == sid).values(legs=legs))


# -- NULL price end to end (A2) -------------------------------------------------

NO_LIMIT = {
    "MKT": ("paper", {"orderType": "MKT"}, "MARKET", {}),
    "STP": ("live", STP, "STOP", {"aux_price": "510.5"}),
    "MIT": ("live", {"orderType": "MIT", "auxPrice": 490.5}, "MARKET_IF_TOUCHED", {"aux_price": "490.5"}),
    "TRAIL": ("live", TRAIL, "TRAILING_STOP", {"trail_value": "1.5"}),
    "TRAIL LIMIT": ("live", TRAIL_LIMIT, "TRAILING_STOP_LIMIT", {"trail_value": "2.5", "trail_spread": "0.05"}),
}


@pytest.mark.parametrize("label", list(NO_LIMIT))
def test_a_no_limit_type_keeps_a_null_price_from_reservation_to_get(futu, live_types, label):
    env, fields, futu_type, terms = NO_LIMIT[label]
    scope = SCOPE if env == "paper" else LIVE

    resp = _post(futu, "/orders/place", _typed(f"nl-{label}", **fields), env=env)

    assert resp.status_code == 200, resp.text
    sid, oid = resp.json()["submission_id"], resp.json()["brokerOrderId"]
    [kw] = _sent(futu)
    assert kw["order_type"] == futu_type and kw["price"] == futu_orders.PLACEHOLDER_PRICE  # SDK boundary only
    row = _row(sid)
    assert row.limit_price is None and row.broker_order_id == oid
    intent = row.legs["futu_intent"]
    assert intent["order_type"] == label and intent["session"] == "RTH"
    assert row.legs["futu_terms"] == {"aux_price": None, "trail_value": None, "trail_spread": None, **terms}
    assert {k: intent[k] for k in terms} == terms

    futu.ctx.book[oid]["price"] = 9.99  # Futu's placeholder echo must never become a limit
    assert _observe(scope, futu.ctx)["complete"]
    assert _row(sid).limit_price is None
    [open_row] = [o for o in orders_payload_for_scope(scope)["open_orders"] if o["brokerOrderId"] == oid]
    assert open_row["limitPrice"] is None


def test_a_no_limit_type_fails_closed_at_the_real_quote_gate_until_its_rules_exist(futu_ib_quotes):
    """The LMT band is never run on a missing limit (X5 task 3 adds per-type rules)."""
    resp = _post(futu_ib_quotes, "/orders/place", _typed("mkt-real-gate", orderType="MKT"))

    assert resp.status_code == 400 and resp.json()["reason_code"] == "FUTU_ORDER_TYPE_UNSUPPORTED", resp.text
    assert futu_ib_quotes.ctx.mutations == []
    with get_sync_engine().connect() as conn:
        row = conn.execute(order_submissions.select()).first()
    assert row.state == "REJECTED" and row.limit_price is None  # reservation released


@pytest.mark.parametrize(
    "env, fields",
    [
        ("paper", {"orderType": "MKT", "limitPrice": 500.2}),  # extra limit
        ("paper", {}),  # LMT without a limit
        ("live", {"orderType": "STP"}),  # no trigger
        ("live", {"orderType": "STP", "auxPrice": 510.5, "limitPrice": 511}),  # STP has no limit
        ("live", {**TRAIL_LIMIT, "limitPrice": 500.0}),  # TRAIL LIMIT has no fixed limit (A3)
        ("live", {"orderType": "TRAIL", "trailType": "RATIO"}),  # no trail value
        ("live", {"limitPrice": 500.2, "auxPrice": 510.5}),  # LMT has no trigger
        ("live", {"limitPrice": 500.2, "trailSpread": 0}),  # LMT has no spread
        ("live", {"orderType": "STP", "auxPrice": "abc"}),  # not a number
        ("live", {"orderType": 5}),  # not a label
    ],
)
def test_a_missing_or_extra_field_is_invalid_body_before_any_io(futu, live_types, env, fields):
    resp = _post(futu, "/orders/place", _typed("bad-body", **fields), env=env)

    assert resp.status_code == 400 and resp.json()["reason_code"] == "INVALID_ORDER_BODY", resp.text
    assert futu.ctx.mutations == [] and futu.ctx.calls == []  # no OpenD round trip
    with get_sync_engine().connect() as conn:
        assert conn.execute(order_submissions.select()).first() is None  # nothing reserved


def _ib_body(**extra):
    return {
        "type": "stock",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": 500.2,
        "client_attempt_id": "ib-x5",
        **extra,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"orderType": "MKT"},
        {"orderType": "STP LMT"},
        {"auxPrice": 510.5},
        {"trailType": "RATIO"},
        {"trailValue": 1.5},
        {"trailSpread": 0},
        {"session": "ETH"},
        {"session": "RTH"},  # any session field: IB has none, never stripped
        {"limitPrice": None},  # IB still requires a price (no "0" default)
    ],
)
def test_an_ib_body_with_a_futu_field_or_no_price_is_refused_before_any_io(extra):
    resp = asyncio.run(order_entry._prepare_place(_ib_body(**extra)))

    assert resp.status_code == 400
    assert json.loads(resp.body)["reason_code"] == "INVALID_ORDER_BODY"


def test_an_ib_body_may_name_lmt():
    assert order_entry._ib_body_refusal(_ib_body(orderType="LMT")) is None
    assert order_entry._ib_body_refusal(_ib_body()) is None


# -- Idempotency on the frozen intent (A1, R2-1) -----------------------------------


def test_an_identical_replay_is_a_duplicate_and_sends_once(futu, live_types):
    first = _post(futu, "/orders/place", _typed("same-1", **STP), env="live")
    again = _post(futu, "/orders/place", _typed("same-1", **{**STP, "auxPrice": "510.50"}), env="live")

    assert first.status_code == 200, first.text
    assert again.status_code == 200 and again.json()["submission_id"] == first.json()["submission_id"], again.text
    assert "duplicate_of" in again.json()
    assert len(_sent(futu)) == 1


@pytest.mark.parametrize(
    "first, replay",
    [
        (STP, {"orderType": "MIT", "auxPrice": 510.5}),  # type
        (STP, {**STP, "auxPrice": 510.6}),  # trigger
        (TRAIL, {**TRAIL, "trailValue": 1.6}),  # trail value
        (TRAIL, {**TRAIL, "trailType": "AMOUNT"}),  # trail type
        (TRAIL_LIMIT, {**TRAIL_LIMIT, "trailSpread": 0.06}),  # trail spread
        ({"limitPrice": 500.2}, {"limitPrice": 500.2, "session": "ETH"}),  # session
        ({"limitPrice": 500.2}, {"orderType": "STP LMT", "auxPrice": 500.0, "limitPrice": 500.2}),  # LMT → STP LMT
    ],
)
def test_a_replay_with_another_type_trigger_trail_or_session_is_a_body_mismatch(futu, live_types, first, replay):
    a = _post(futu, "/orders/place", _typed("changed-1", **first), env="live")
    b = _post(futu, "/orders/place", _typed("changed-1", **replay), env="live")

    assert a.status_code == 200, a.text
    assert b.status_code == 409 and b.json()["reason_code"] == "ATTEMPT_ALREADY_PLACED", b.text
    assert b.json()["submission_id"] == a.json()["submission_id"]
    assert len(_sent(futu)) == 1  # the new terms were never sent
    intent = _row(a.json()["submission_id"]).legs["futu_intent"]
    assert intent == futu_orders.intent_for(order_entry._futu_spec(_typed("x", **first)))


def _confirm_amend(sid, scope, **terms):
    """A confirmed trigger / trail amend as the modify path leaves it: the
    amend generation settled and `futu_terms` moved (X5 task 3 writes them
    through settle_modify; the intent is never touched)."""
    scope_d = scope.as_dict()
    opened = gate4.begin_modify(sid, None, scope_d)
    assert opened.accept
    assert gate4.settle_modify(sid, opened.amend_id, "confirmed", scope_d)
    _set_legs(sid, futu_terms={**_row(sid).legs["futu_terms"], **terms})


@pytest.mark.parametrize(
    "first, field, book_key, amended",
    [
        (STP, "auxPrice", "aux_price", "511"),
        (TRAIL, "trailValue", "trail_value", "1.8"),
        (TRAIL_LIMIT, "trailSpread", "trail_spread", "0.07"),
    ],
)
def test_a_replay_after_a_confirmed_amend_still_matches_the_placement(
    futu, live_types, first, field, book_key, amended
):
    a = _post(futu, "/orders/place", _typed("amended-1", **first), env="live")
    sid, oid = a.json()["submission_id"], a.json()["brokerOrderId"]
    intent = _row(sid).legs["futu_intent"]
    futu.ctx.book[oid][book_key] = float(amended)  # the broker order moved
    _confirm_amend(sid, LIVE, **{book_key: amended})
    assert _observe(LIVE, futu.ctx)["complete"]  # a bound order with new terms is not refused

    row = _row(sid)
    assert row.legs["futu_intent"] == intent  # frozen through settle and observation
    assert row.legs["futu_terms"][book_key] == amended and row.legs.get("pending_amend_id") is None
    original = _post(futu, "/orders/place", _typed("amended-1", **first), env="live")
    assert original.status_code == 200 and original.json()["submission_id"] == sid, original.text
    altered = _post(futu, "/orders/place", _typed("amended-1", **{**first, field: float(amended)}), env="live")
    assert altered.status_code == 409 and altered.json()["reason_code"] == "ATTEMPT_ALREADY_PLACED"
    assert len(_sent(futu)) == 1


# -- Binding: restart after an uncertain send, contradictory remark (A1) -----------

OID = "8812345678900009999"


@pytest.mark.parametrize(
    "env, fields, booked, tamper, bound",
    [
        ("paper", {"orderType": "MKT"}, {"order_type": "MARKET", "price": 0.0}, {}, True),
        ("paper", {"orderType": "MKT"}, {"order_type": "MARKET", "price": 0.0}, {"order_type": "STOP"}, False),
        ("paper", {"orderType": "MKT"}, {"order_type": "MARKET", "price": 0.0}, {"qty": 3.0}, False),
        ("paper", {"orderType": "MKT"}, {"order_type": "MARKET", "price": 0.0}, {"aux_price": 401.0}, False),
        ("paper", {"orderType": "MKT"}, {"order_type": "MARKET", "price": 0.0}, {"session": "OVERNIGHT"}, False),
        ("paper", {"orderType": "MKT"}, {"order_type": "MARKET", "price": 0.0}, {"session": "RTH"}, True),
        ("live", STP, {"order_type": "STOP", "price": 0.0, "aux_price": 510.5}, {"aux_price": 510.50000001}, True),
        ("live", STP, {"order_type": "STOP", "price": 0.0, "aux_price": 510.5}, {"aux_price": 510.51}, False),
        (
            "live",
            TRAIL,
            {"order_type": "TRAILING_STOP", "price": 0.0, "trail_type": "RATIO", "trail_value": 1.5},
            {},
            True,
        ),
        (
            "live",
            TRAIL,
            {"order_type": "TRAILING_STOP", "price": 0.0, "trail_type": "RATIO", "trail_value": 1.5},
            {"trail_type": "AMOUNT"},
            False,
        ),
        (
            "live",
            TRAIL_LIMIT,
            {
                "order_type": "TRAILING_STOP_LIMIT",
                "price": 0.0,
                "trail_type": "AMOUNT",
                "trail_value": 2.5,
                "trail_spread": 0.05,
            },
            {"trail_spread": 0.06},
            False,
        ),
    ],
)
def test_after_an_uncertain_place_and_a_restart_only_the_persisted_placement_binds(
    futu, live_types, env, fields, booked, tamper, bound
):
    scope, acc = (SCOPE, ACC) if env == "paper" else (LIVE, LIVE_ACC)
    futu.ctx.place_mode = "lost"  # possibly sent; no order found by remark yet
    resp = _post(futu, "/orders/place", _typed("restart-1", **fields), env=env)
    assert resp.status_code == 502 and resp.json()["reason_code"] == "PLACE_OUTCOME_UNKNOWN", resp.text
    sid = resp.json()["submission_id"]
    assert _row(sid).state == "UNKNOWN"

    # Restart: a new process has only Postgres. The order did reach Futu.
    order = {**_order(order_id=OID, code="US.SPY", side="BUY", qty=2.0, remark=f"xo:{sid}"), **booked, **tamper}
    ctx = FakeTradeCtx(open_orders=[order], accounts=[_acc_row(), _acc_row(LIVE_ACC, env="REAL")])
    result = _observe(scope, ctx)

    row = _row(sid)
    assert len(_sent(futu)) == 1  # never re-sent
    if bound:
        assert result["complete"], result
        assert row.broker_order_id == OID and row.state == "WORKING" and row.limit_price is None
    else:
        assert not result["complete"]  # Gate 4 fails closed on the unbound order
        assert row.broker_order_id is None and row.state == "UNKNOWN"
        assert _rows_for(OID) == 0  # not imported as an outside order either
    assert row.broker_account == acc


@pytest.mark.parametrize("mode", ["ok", "error_after_send"])
def test_a_remark_match_with_other_terms_is_never_bound_on_the_place_path(futu, live_types, mode):
    """Accepted reply + fresh read, or remark recovery after an uncertain
    send: Futu's order carries the remark but another trigger → UNKNOWN."""
    futu.ctx.place_mode = mode
    place = futu.ctx.place_order

    def tampered(**kw):
        out = place(**kw)
        next(iter(futu.ctx.book.values()))["aux_price"] = 505.0
        return out

    futu.ctx.place_order = tampered
    resp = _post(futu, "/orders/place", _typed("tampered-1", **STP), env="live")

    assert resp.status_code == 502 and resp.json()["reason_code"] == "PLACE_OUTCOME_UNKNOWN", resp.text
    row = _row(resp.json()["submission_id"])
    assert row.state == "UNKNOWN" and row.broker_order_id is None
    assert len(_sent(futu)) == 1


# -- Legacy Xenon rows and outside orders (R2-2) -----------------------------------


def test_a_pre_x5_xenon_lmt_row_keeps_replay_bind_and_modify(futu):
    sid, oid = _placed(futu, cid="legacy-1")
    _set_legs(sid, futu_intent=None, futu_terms=None)  # as v0.16 stored it
    assert orders_store.futu_intent_of(_row(sid)) == orders_store.LEGACY_FUTU_INTENT

    replay = _post(futu, "/orders/place", _stock(cid="legacy-1"))
    assert replay.status_code == 200 and replay.json()["submission_id"] == sid, replay.text
    changed = _post(futu, "/orders/place", _typed("legacy-1", orderType="MKT"))
    assert changed.status_code == 409 and changed.json()["reason_code"] == "ATTEMPT_ALREADY_PLACED"

    mod = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 1})
    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    [kw] = _modifies(futu)
    assert kw["price"] == 500.1 and not {"aux_price", "trail_type", "session", "order_type"} & set(kw)


@pytest.mark.parametrize("booked, bound", [("NORMAL", True), ("STOP", False)])
def test_a_pre_x5_unknown_xenon_row_binds_only_a_limit_order(futu, booked, bound):
    futu.ctx.place_mode = "lost"
    sid = _post(futu, "/orders/place", _stock(cid="legacy-unknown")).json()["submission_id"]
    _set_legs(sid, futu_intent=None, futu_terms=None)
    order = {**_order(order_id=OID, code="US.SPY", side="BUY", qty=2.0, remark=f"xo:{sid}"), "order_type": booked}

    result = _observe(SCOPE, FakeTradeCtx(open_orders=[order]))

    assert result["complete"] is bound
    assert (_row(sid).broker_order_id == OID) is bound


EXTERNAL = {
    "STP LMT": {"order_type": "STOP_LIMIT", "price": 511.0, "aux_price": 510.5},
    "TRAIL": {"order_type": "TRAILING_STOP", "price": 3.0, "trail_type": "RATIO", "trail_value": 1.5},
    "OVERNIGHT LMT": {"order_type": "NORMAL", "price": 500.2, "session": "OVERNIGHT"},
}
EXT_OID = "8812345678900007777"


def _import_external(futu, cols, *, pre_upgrade=False):
    """An order made in the Futu app, imported by the observation mirror.
    `pre_upgrade`: stored as v0.16 did (the SDK price as limit, no X5 fields)."""
    order = {**_order(order_id=EXT_OID, code="US.SPY", side="BUY", qty=1.0), **cols}
    futu.ctx.book[EXT_OID] = order
    obs = fo.observation_from_row(order, LIVE, datetime.now(timezone.utc) - timedelta(seconds=5))
    out = orders_store.mirror_futu_order(
        obs, broker_account=LIVE_ACC, account_env="live", observed_at=datetime.now(timezone.utc) - timedelta(seconds=5)
    )
    sid = out["submission_id"]
    if pre_upgrade:
        with get_sync_engine().begin() as conn:
            conn.execute(
                update(order_submissions)
                .where(order_submissions.c.submission_id == sid)
                .values(limit_price=Decimal(str(cols["price"])))
            )
    row = _row(sid)
    assert row.user_id == orders_store.FUTU_OBSERVATION_USER and "futu_intent" not in row.legs
    assert orders_store.futu_intent_of(row) is None  # no fabricated fingerprint
    return sid


@pytest.mark.parametrize("pre_upgrade", [False, True])
@pytest.mark.parametrize("label", list(EXTERNAL))
def test_an_outside_order_is_managed_from_a_fresh_read(futu, live_types, monkeypatch, label, pre_upgrade):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", "LMT,STP LMT,TRAIL")
    sid = _import_external(futu, EXTERNAL[label], pre_upgrade=pre_upgrade)

    mod = _post(futu, "/orders/modify", {"brokerOrderId": EXT_OID, "newQuantity": 2, "modifySequence": 1}, env="live")

    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    [kw] = _modifies(futu)
    assert kw["qty"] == 2 and "session" not in kw and "order_type" not in kw  # session / type are place-only
    if label == "STP LMT":
        assert (kw["price"], kw["aux_price"]) == (511.0, 510.5) and mod.json()["confirmedPrice"] == "511.00"
    elif label == "TRAIL":
        assert (kw["price"], kw["trail_type"], kw["trail_value"]) == (0.0, "RATIO", 1.5)
        assert mod.json()["confirmedPrice"] is None  # no fixed limit, nothing compared
    else:
        assert kw["price"] == 500.2 and "aux_price" not in kw
    assert orders_store.futu_intent_of(_row(sid)) is None


@pytest.mark.parametrize(
    "label, env_vars, code",
    [
        ("STP LMT", {}, "FUTU_ORDER_TYPE_UNSUPPORTED"),  # tuple outside the default allowlist
        ("TRAIL", {}, "FUTU_ORDER_TYPE_UNSUPPORTED"),
        ("OVERNIGHT LMT", {}, "FUTU_SESSION_UNSUPPORTED"),  # never treated as RTH
        ("STP LMT", {"XENON_FUTU_TUPLES_LIVE": LIVE_TUPLES}, "FUTU_ORDER_NOT_MODIFIABLE"),  # modify types = LMT
    ],
)
def test_an_outside_order_outside_the_allowlist_keeps_cancel_and_demand(futu, monkeypatch, label, env_vars, code):
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    sid = _import_external(futu, EXTERNAL[label])
    before = _row(sid)

    mod = _post(futu, "/orders/modify", {"brokerOrderId": EXT_OID, "newQuantity": 2, "modifySequence": 1}, env="live")

    assert mod.status_code == 400 and mod.json()["reason_code"] == code, mod.text
    assert _modifies(futu) == []
    after = _row(sid)
    assert (after.state, after.quantity) == (before.state, before.quantity)
    assert after.legs.get("pending_amend_id") is None
    can = _post(futu, "/orders/cancel", {"brokerOrderId": EXT_OID}, env="live")
    assert can.status_code == 200 and can.json()["confirmed"] is True


@pytest.mark.parametrize(
    "cols",
    [
        {"order_type": "STOP_LIMIT", "price": 511.0},  # trigger not reported
        {"order_type": "TRAILING_STOP", "price": 0.0, "trail_value": 1.5},  # trail type not reported
        {"order_type": "AUCTION_LIMIT", "price": 500.2},  # not a Xenon order type
        {"order_type": "N/A", "price": 500.2},  # type not reported
    ],
)
def test_an_outside_order_with_unreadable_terms_is_not_modifiable(futu, live_types, monkeypatch, cols):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", "LMT,STP LMT,TRAIL")
    _import_external(futu, cols)

    mod = _post(futu, "/orders/modify", {"brokerOrderId": EXT_OID, "newQuantity": 2, "modifySequence": 1}, env="live")

    assert mod.status_code == 400 and mod.json()["reason_code"] == "FUTU_ORDER_NOT_MODIFIABLE", mod.text
    assert _modifies(futu) == []
    can = _post(futu, "/orders/cancel", {"brokerOrderId": EXT_OID}, env="live")
    assert can.status_code == 200 and can.json()["confirmed"] is True


def test_a_pre_upgrade_outside_trail_order_reads_and_stores_no_limit(futu):
    sid = _import_external(futu, EXTERNAL["TRAIL"], pre_upgrade=True)
    [open_row] = [o for o in orders_payload_for_scope(LIVE)["open_orders"] if o["brokerOrderId"] == EXT_OID]
    assert open_row["limitPrice"] is None  # the v0.16 column value is an SDK placeholder

    assert _observe(LIVE, futu.ctx)["complete"]  # a fresh observation clears the column
    assert _row(sid).limit_price is None


# -- Modify of a Xenon no-limit order --------------------------------------------


def test_a_xenon_stop_modify_resends_the_read_trigger_and_no_limit(futu, live_types, monkeypatch):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", "LMT,STP")
    sid, oid = (lambda r: (r["submission_id"], r["brokerOrderId"]))(
        _post(futu, "/orders/place", _typed("stp-mod", **STP), env="live").json()
    )
    futu.ctx.book[oid]["aux_price"] = 512.0  # changed in the Futu app: an omitted field keeps Futu's value

    mod = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newQuantity": 3, "modifySequence": 1}, env="live")

    assert mod.status_code == 200, mod.text
    assert mod.json()["confirmed"] is True and mod.json()["confirmedQuantity"] == 3
    assert mod.json()["confirmedPrice"] is None
    [kw] = _modifies(futu)
    assert (kw["qty"], kw["price"], kw["aux_price"]) == (3, futu_orders.PLACEHOLDER_PRICE, 512.0)
    row = _row(sid)
    assert row.quantity == 3 and row.limit_price is None
    assert row.legs["futu_intent"]["aux_price"] == "510.5"  # the placement never changes


def test_a_new_limit_on_a_no_limit_order_is_refused_before_the_sequence(futu, live_types, monkeypatch):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", "LMT,STP")
    resp = _post(futu, "/orders/place", _typed("stp-price", **STP), env="live").json()

    mod = _post(
        futu,
        "/orders/modify",
        {"brokerOrderId": resp["brokerOrderId"], "newPrice": 511, "modifySequence": 1},
        env="live",
    )

    assert mod.status_code == 400 and mod.json()["reason_code"] == "INVALID_ORDER_BODY", mod.text
    assert _modifies(futu) == [] and _row(resp["submission_id"]).modify_sequence == 0


def test_a_xenon_order_whose_read_shows_another_type_is_not_modifiable(futu):
    sid, oid = _placed(futu, cid="type-drift")
    futu.ctx.book[oid]["order_type"] = "STOP"

    mod = _post(futu, "/orders/modify", {"brokerOrderId": oid, "newPrice": 500.1, "modifySequence": 1})

    assert mod.status_code == 400 and mod.json()["reason_code"] == "FUTU_ORDER_NOT_MODIFIABLE", mod.text
    assert _modifies(futu) == [] and _row(sid).legs.get("pending_amend_id") is None
    assert _post(futu, "/orders/cancel", {"brokerOrderId": oid}).json()["confirmed"] is True


# -- Units: intent text, bind comparison, observation parse -------------------------


def test_intent_values_are_canonical_text():
    spec = futu_orders.FutuOrderSpec(
        sec_type="STK",
        symbol="SPY",
        action="BUY",
        quantity=1,
        limit_price=None,
        order_type="TRAIL LIMIT",
        trail_type="AMOUNT",
        trail_value=Decimal("1E+1"),
        trail_spread=Decimal("0.050"),
    )
    assert futu_orders.intent_for(spec) == {
        "order_type": "TRAIL LIMIT",
        "aux_price": None,
        "trail_type": "AMOUNT",
        "trail_value": "10",
        "trail_spread": "0.05",
        "session": "RTH",
    }


class _Row(dict):
    """A row both by key (`futu_intent_of`) and by attribute (the binder)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _xrow(intent, sec="STK", qty=2):
    return _Row(
        broker="FUTU",
        user_id="local",
        submission_id="x5-sid",
        legs={"futu_intent": intent} if intent is not None else {},
        quantity=qty,
        security_type=sec,
    )


def _obs(**kw):
    base = dict(
        order_id="1",
        remark=None,
        ticker="SPY",
        security_type="STK",
        action="BUY",
        quantity=2,
        limit_price=None,
        state="WORKING",
        legs={},
        dealt_qty=0,
        dealt_avg_price=None,
        updated_at=datetime.now(timezone.utc),
    )
    return orders_store.FutuOrderObservation(**{**base, **kw})


def _intent(**kw):
    return {**orders_store.LEGACY_FUTU_INTENT, **kw}


D = Decimal


@pytest.mark.parametrize(
    "intent, sec, obs, want",
    [
        (_intent(), "STK", dict(order_type="NORMAL"), None),
        (_intent(), "STK", dict(order_type="NORMAL", trail_spread=D(0)), None),  # zero spread is no term
        (_intent(), "STK", dict(order_type="NORMAL", aux_price=D(1)), "aux_price"),  # LMT has no trigger
        (_intent(), "STK", dict(order_type="NORMAL", quantity=3), "quantity"),
        (_intent(), "STK", dict(order_type="ABSOLUTE_LIMIT"), "order_type"),  # unmapped ≠ LMT
        (_intent(), "STK", dict(order_type=None), None),  # not reported: no contradiction
        (_intent(), "STK", dict(session="RTH"), None),
        (_intent(), "STK", dict(session="OVERNIGHT"), "session"),
        (_intent(session="ETH"), "STK", dict(session=None), None),
        (
            _intent(order_type="STP", aux_price="510.5"),
            "STK",
            dict(order_type="STOP", aux_price=D("510.50000001")),
            None,
        ),
        (
            _intent(order_type="STP", aux_price="510.5"),
            "STK",
            dict(order_type="STOP", aux_price=D("510.51")),
            "aux_price",
        ),
        (_intent(order_type="STP", aux_price="510.5"), "STK", dict(order_type="STOP"), None),  # trigger not reported
        (
            _intent(order_type="STP", aux_price="0.5123"),
            "STK",
            dict(aux_price=D("0.51230001")),
            None,
        ),  # sub-dollar grid
        (_intent(order_type="STP", aux_price="0.5123"), "STK", dict(aux_price=D("0.5124")), "aux_price"),
        (
            _intent(order_type="TRAIL", trail_type="RATIO", trail_value="1.25"),
            "STK",
            dict(trail_value=D("1.2500001")),
            None,
        ),
        (
            _intent(order_type="TRAIL", trail_type="RATIO", trail_value="1.25"),
            "STK",
            dict(trail_value=D("1.26")),
            "trail_value",
        ),
        (
            _intent(order_type="TRAIL", trail_type="RATIO", trail_value="1.25"),
            "STK",
            dict(trail_type="AMOUNT"),
            "trail_type",
        ),
        (
            _intent(order_type="TRAIL", trail_type="AMOUNT", trail_value="0.1234"),
            "STK",
            dict(trail_value=D("0.12341")),
            None,
        ),
        (
            _intent(order_type="TRAIL", trail_type="AMOUNT", trail_value="0.12"),
            "OPT",
            dict(trail_value=D("0.121")),
            None,
        ),
        (
            _intent(order_type="TRAIL", trail_type="AMOUNT", trail_value="0.12"),
            "OPT",
            dict(trail_value=D("0.13")),
            "trail_value",
        ),
        (_intent(order_type="TRAIL LIMIT", trail_spread="0"), "STK", dict(trail_spread=D(0)), None),
        (_intent(order_type="TRAIL LIMIT", trail_spread="0"), "STK", dict(trail_spread=D("0.001")), "trail_spread"),
        (None, "STK", dict(order_type="STOP", aux_price=D(5)), None),  # outside order: no intent
        (None, "STK", dict(quantity=3), "quantity"),
    ],
)
def test_bind_comparison_per_term(intent, sec, obs, want):
    row = _xrow(intent, sec)
    if intent is None:
        row["user_id"] = orders_store.FUTU_OBSERVATION_USER
    assert orders_store._futu_intent_mismatch(row, _obs(security_type=sec, **obs)) == want


@pytest.mark.parametrize(
    "futu_type, limit",
    [
        ("NORMAL", D("9.99")),
        ("STOP_LIMIT", D("9.99")),
        ("LIMIT_IF_TOUCHED", D("9.99")),
        ("AUCTION_LIMIT", D("9.99")),  # not a Xenon type: kept as observed
        ("MARKET", None),
        ("STOP", None),
        ("MARKET_IF_TOUCHED", None),
        ("TRAILING_STOP", None),
        ("TRAILING_STOP_LIMIT", None),
    ],
)
def test_observation_stores_no_limit_for_a_type_without_one(futu_type, limit):
    row = {
        **_order(order_id="1", code="US.SPY", side="BUY", qty=1.0),
        "order_type": futu_type,
        "price": 9.99,
        "trail_type": "RATIO",
        "trail_value": 1.5,
        "trail_spread": 0.0,
        "session": "OVERNIGHT",
    }
    obs = fo.observation_from_row(row, SCOPE, datetime.now(timezone.utc))
    assert obs.limit_price == limit
    assert (obs.trail_type, obs.trail_value, obs.trail_spread, obs.session) == (
        "RATIO",
        D("1.5"),
        D("0.0"),
        "OVERNIGHT",
    )
    na = fo.observation_from_row(
        _order(order_id="2", code="US.SPY", side="BUY", qty=1.0), SCOPE, datetime.now(timezone.utc)
    )
    assert (na.trail_type, na.trail_value, na.trail_spread, na.session) == (None, None, None, None)  # 'N/A'
