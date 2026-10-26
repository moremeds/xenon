"""X5a task 3: per-type IB quote rules on place and modify (plan v4 § 4, § 5).

First the pure rules (`quote_guard.check_terms`), then the real routes, Gate 4,
the Futu order worker and the autospec-bound fake Futu context
(`FakeOrderCtx`) with only the IB quote fetch stubbed (`quotes`). Bid / ask,
ids and prices are made-up test values, not market data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from scripts.tests import test_orders_futu_branch as _branch
from scripts.tests.test_futu_observation import _env  # noqa: F401 — autouse fixture
from scripts.tests.test_futu_order_intent import LIVE_TUPLES, STP, TRAIL_LIMIT, _typed
from scripts.tests.test_orders_futu_branch import (  # noqa: F401 — autouse fixture
    _configured,
    _modifies,
    _post,
    _row,
)
from xenon.api.services import order_entry
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_events
from xenon.execution import orders_store, quote_guard
from xenon.execution.preflight import ReasonCode
from xenon.execution.quote_tokens import QuotePayload

pytestmark = pytest.mark.committed_db

futu_ib_quotes = _branch.futu_ib_quotes

RTH = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)  # Wed 11:00 ET
CLOSED = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)  # Tue 22:00 ET
HOLIDAY = datetime(2026, 1, 19, 15, 0, tzinfo=timezone.utc)  # MLK day, 10:00 ET
BID, ASK = Decimal("500.10"), Decimal("500.30")
CON_ID = 756733


# -- Pure rules -----------------------------------------------------------------


def _check(order_type, action="BUY", *, bid=BID, ask=ASK, size=100, now=RTH, cap=Decimal(25000), sec="STK", **t):
    payload = QuotePayload(con_id=CON_ID, ticker="SPY", bid=bid, ask=ask, bid_size=size, ask_size=size, ts_server_ms=0)
    terms = {k: Decimal(str(v)) if isinstance(v, (int, float, str)) and k != "trail_type" else v for k, v in t.items()}
    return [
        v.reason_code.value
        for v in quote_guard.check_terms(
            payload=payload,
            con_id=CON_ID,
            ticker="SPY",
            security_type=sec,
            action=action,
            order_type=order_type,
            now=now,
            tick_rule_lookup=lambda _cid: Decimal("0.01"),
            mkt_notional_cap=cap,
            **terms,
        )
    ]


@pytest.mark.parametrize(
    "order_type, action, kw, codes",
    [
        # MKT: RTH by the calendar, spread <= 1 % of mid, qty x ask <= cap; no override.
        ("MKT", "BUY", {"quantity": 2}, []),
        ("MKT", "SELL", {"quantity": 2}, []),
        ("MKT", "BUY", {"quantity": 2, "now": CLOSED}, ["FUTU_SESSION_UNSUPPORTED"]),
        ("MKT", "BUY", {"quantity": 2, "now": HOLIDAY}, ["FUTU_SESSION_UNSUPPORTED"]),
        ("MKT", "BUY", {"quantity": 2, "bid": Decimal("495"), "ask": Decimal("505")}, ["MKT_SPREAD_TOO_WIDE"]),
        ("MKT", "BUY", {"quantity": 49}, []),  # 49 x 500.30 = 24 514.70
        ("MKT", "BUY", {"quantity": 50}, ["MKT_NOTIONAL_CAP"]),  # 25 015.00
        ("MKT", "BUY", {"quantity": 1, "cap": None}, ["MKT_NOTIONAL_CAP"]),  # bad config: fail closed
        # STP / MIT: the trigger must not fire at once; no override.
        ("STP", "BUY", {"aux_price": "500.31"}, []),
        ("STP", "BUY", {"aux_price": "500.30"}, ["TRIGGER_WOULD_FIRE"]),
        ("STP", "SELL", {"aux_price": "500.09"}, []),
        ("STP", "SELL", {"aux_price": "500.10"}, ["TRIGGER_WOULD_FIRE"]),
        ("MIT", "BUY", {"aux_price": "500.09"}, []),
        ("MIT", "BUY", {"aux_price": "500.10"}, ["TRIGGER_WOULD_FIRE"]),
        ("MIT", "SELL", {"aux_price": "500.31"}, []),
        ("MIT", "SELL", {"aux_price": "500.30"}, ["TRIGGER_WOULD_FIRE"]),
        ("STP", "BUY", {"aux_price": "500.315"}, ["LIMIT_OFF_TICK"]),
        # STP LMT / LIT: trigger rule, then the limit judged against the trigger.
        ("STP LMT", "BUY", {"aux_price": "510.50", "limit_price": "510.50"}, []),
        ("STP LMT", "BUY", {"aux_price": "510.50", "limit_price": "510.52"}, []),  # aux + 2 ticks
        ("STP LMT", "BUY", {"aux_price": "510.50", "limit_price": "510.53"}, ["LIMIT_OUT_OF_BAND"]),
        ("STP LMT", "BUY", {"aux_price": "510.50", "limit_price": "510.49"}, ["LIMIT_OUT_OF_BAND"]),
        ("STP LMT", "SELL", {"aux_price": "490.50", "limit_price": "490.48"}, []),  # aux - 2 ticks
        ("STP LMT", "SELL", {"aux_price": "490.50", "limit_price": "490.47"}, ["LIMIT_OUT_OF_BAND"]),
        ("STP LMT", "SELL", {"aux_price": "490.50", "limit_price": "490.51"}, ["LIMIT_OUT_OF_BAND"]),
        ("STP LMT", "BUY", {"aux_price": "500.00", "limit_price": "500.01"}, ["TRIGGER_WOULD_FIRE"]),
        ("STP LMT", "BUY", {"aux_price": "500.00", "limit_price": "501"}, ["TRIGGER_WOULD_FIRE", "LIMIT_OUT_OF_BAND"]),
        ("LIT", "BUY", {"aux_price": "490.50", "limit_price": "490.52"}, []),
        ("LIT", "BUY", {"aux_price": "500.20", "limit_price": "500.20"}, ["TRIGGER_WOULD_FIRE"]),
        ("LIT", "SELL", {"aux_price": "510.50", "limit_price": "510.48"}, []),
        ("LIT", "SELL", {"aux_price": "500.20", "limit_price": "500.20"}, ["TRIGGER_WOULD_FIRE"]),
        ("LIT", "BUY", {"aux_price": "490.50", "limit_price": "490.60"}, ["LIMIT_OUT_OF_BAND"]),
        ("LIT", "BUY", {"aux_price": "490.50", "limit_price": "490.505"}, ["LIMIT_OFF_TICK"]),
        # A low-priced stock: 5 % is the tighter bound, not 2 ticks.
        (
            "STP LMT",
            "BUY",
            {"aux_price": "0.21", "limit_price": "0.22", "bid": Decimal("0.18"), "ask": Decimal("0.19")},
            [],
        ),
        (
            "STP LMT",
            "BUY",
            {"aux_price": "0.20", "limit_price": "0.22", "bid": Decimal("0.18"), "ask": Decimal("0.19")},
            ["LIMIT_OUT_OF_BAND"],
        ),
        # TRAIL / TRAIL LIMIT: AMOUNT <= 20 % and spread <= 5 % of the IB mid (500.20).
        ("TRAIL", "BUY", {"trail_type": "RATIO", "trail_value": "20"}, []),
        ("TRAIL", "SELL", {"trail_type": "AMOUNT", "trail_value": "100.04"}, []),
        ("TRAIL", "SELL", {"trail_type": "AMOUNT", "trail_value": "100.05"}, ["FUTU_INVALID_ORDER"]),
        ("TRAIL LIMIT", "BUY", {"trail_type": "AMOUNT", "trail_value": "2.5", "trail_spread": "25.01"}, []),
        (
            "TRAIL LIMIT",
            "BUY",
            {"trail_type": "AMOUNT", "trail_value": "2.5", "trail_spread": "25.011"},
            ["FUTU_INVALID_ORDER"],
        ),
        ("TRAIL LIMIT", "BUY", {"trail_type": "RATIO", "trail_value": "1.5", "trail_spread": "0"}, []),
        # Every type needs a sized, non-crossed quote, also outside RTH.
        ("STP", "BUY", {"aux_price": "510", "bid": Decimal("500.4")}, ["QUOTE_UNAVAILABLE"]),
        ("TRAIL", "BUY", {"trail_type": "RATIO", "trail_value": "1", "size": 0}, ["QUOTE_UNAVAILABLE"]),
        ("MIT", "BUY", {"aux_price": "490", "bid": Decimal("0")}, ["QUOTE_UNAVAILABLE"]),
        ("STP", "BUY", {"aux_price": "510", "now": CLOSED}, []),  # a GTC / ETH trigger may rest overnight
        (
            "STP LMT",
            "BUY",
            {"aux_price": "510", "limit_price": "510", "sec": "OPT", "now": CLOSED},
            ["OPTION_MARKET_CLOSED"],
        ),
        # LMT keeps the existing band.
        ("LMT", "BUY", {"limit_price": "500.32"}, []),
        ("LMT", "BUY", {"limit_price": "500.33"}, ["LIMIT_OUT_OF_BAND"]),
        ("PEG", "BUY", {}, ["FUTU_ORDER_TYPE_UNSUPPORTED"]),
    ],
)
def test_per_type_quote_rules(order_type, action, kw, codes):
    assert _check(order_type, action, **kw) == codes


@pytest.mark.parametrize(
    "order_type, code, ok",
    [
        ("LMT", ReasonCode.LIMIT_OUT_OF_BAND, True),
        ("STP LMT", ReasonCode.LIMIT_OUT_OF_BAND, True),
        ("LIT", ReasonCode.TRIGGER_WOULD_FIRE, True),
        ("STP LMT", ReasonCode.TRIGGER_WOULD_FIRE, True),
        ("STP", ReasonCode.TRIGGER_WOULD_FIRE, False),
        ("MIT", ReasonCode.TRIGGER_WOULD_FIRE, False),
        ("MKT", ReasonCode.MKT_SPREAD_TOO_WIDE, False),
        ("MKT", ReasonCode.MKT_NOTIONAL_CAP, False),
        ("TRAIL", ReasonCode.FUTU_INVALID_ORDER, False),
        ("LMT", ReasonCode.QUOTE_UNAVAILABLE, False),
    ],
)
def test_only_band_and_limit_trigger_refusals_are_overridable(order_type, code, ok):
    assert quote_guard.overridable(order_type, quote_guard.QuoteVerdict(accept=False, reason_code=code)) is ok


@pytest.mark.parametrize(
    "raw, cap",
    [(None, Decimal(25000)), ("", Decimal(25000)), ("1000.5", Decimal("1000.5")), ("0", None), ("-1", None)]
    + [("abc", None), ("NaN", None), ("inf", None)],
)
def test_mkt_notional_cap_config_fails_closed(raw, cap):
    environ = {} if raw is None else {quote_guard.MKT_NOTIONAL_CAP_ENV: raw}
    assert quote_guard.mkt_notional_cap(environ) == cap


# -- Routes: place ----------------------------------------------------------------


@pytest.fixture
def quotes(futu_ib_quotes, monkeypatch):
    """The real quote gate on place and modify; the IB snapshot is `quotes.snap`."""
    monkeypatch.setenv("XENON_FUTU_TUPLES_LIVE", LIVE_TUPLES)
    monkeypatch.setattr(order_entry, "_now", lambda: RTH)
    futu_ib_quotes.snap = {"bid": BID, "ask": ASK, "bid_size": 100, "ask_size": 100}
    futu_ib_quotes.fetches = []

    async def _snapshot(body):
        futu_ib_quotes.fetches.append(body)
        if futu_ib_quotes.snap is None:
            raise order_entry.HTTPException(status_code=503, detail="IB data role unavailable")
        return CON_ID, dict(futu_ib_quotes.snap)

    monkeypatch.setattr(order_entry, "_fetch_order_quote_snapshot", _snapshot)
    return futu_ib_quotes


def _events(sid, kind):
    with get_sync_engine().connect() as conn:
        return [
            r.detail
            for r in conn.execute(
                select(order_events.c.detail).where(
                    (order_events.c.submission_id == sid) & (order_events.c.kind == kind)
                )
            )
        ]


def _only_row():
    from xenon.db.schema import order_submissions

    with get_sync_engine().connect() as conn:
        [row] = conn.execute(order_submissions.select()).all()
    return row


STP_LMT = {"orderType": "STP LMT", "auxPrice": 510.5, "limitPrice": 510.52}
LIT = {"orderType": "LIT", "auxPrice": 490.5, "limitPrice": 490.52}
MIT = {"orderType": "MIT", "auxPrice": 490.5}
MKT = {"orderType": "MKT"}


@pytest.mark.parametrize(
    "env, fields, setup, code",
    [
        ("paper", MKT, {}, None),
        ("paper", MKT, {"now": CLOSED}, "FUTU_SESSION_UNSUPPORTED"),
        ("paper", MKT, {"snap": {"bid": Decimal(495), "ask": Decimal(505)}}, "MKT_SPREAD_TOO_WIDE"),
        ("paper", {**MKT, "quantity": 50}, {}, "MKT_NOTIONAL_CAP"),
        ("paper", MKT, {"cap": "1000"}, "MKT_NOTIONAL_CAP"),  # 2 x 500.30 = 1 000.60
        ("paper", MKT, {"cap": "lots"}, "MKT_NOTIONAL_CAP"),
        ("live", STP, {}, None),
        ("live", {**STP, "auxPrice": 500.3}, {}, "TRIGGER_WOULD_FIRE"),
        ("live", MIT, {}, None),
        ("live", {**MIT, "auxPrice": 500.1}, {}, "TRIGGER_WOULD_FIRE"),
        ("live", STP_LMT, {}, None),
        ("live", {**STP_LMT, "auxPrice": 500.2, "limitPrice": 500.2}, {}, "TRIGGER_WOULD_FIRE"),
        ("live", {**STP_LMT, "limitPrice": 510.53}, {}, "LIMIT_OUT_OF_BAND"),
        ("live", LIT, {}, None),
        ("live", {**LIT, "limitPrice": 490.6}, {}, "LIMIT_OUT_OF_BAND"),
        ("live", TRAIL_LIMIT, {}, None),
        ("live", {**TRAIL_LIMIT, "trailValue": 100.05}, {}, "FUTU_INVALID_ORDER"),
        ("live", {**TRAIL_LIMIT, "trailSpread": 25.011}, {}, "FUTU_INVALID_ORDER"),
        ("live", STP, {"snap": {"bid": Decimal("500.4")}}, "QUOTE_UNAVAILABLE"),  # crossed
        ("live", TRAIL_LIMIT, {"snap": {"bid_size": 0}}, "QUOTE_UNAVAILABLE"),
        ("live", STP, {"snap": None}, "QUOTE_UNAVAILABLE"),  # IB down: 503, nothing sent
    ],
)
def test_place_runs_the_rule_of_its_type(quotes, monkeypatch, env, fields, setup, code):
    if "now" in setup:
        monkeypatch.setattr(order_entry, "_now", lambda: setup["now"])
    if "cap" in setup:
        monkeypatch.setenv(quote_guard.MKT_NOTIONAL_CAP_ENV, setup["cap"])
    if "snap" in setup:
        quotes.snap = None if setup["snap"] is None else {**quotes.snap, **setup["snap"]}

    resp = _post(quotes, "/orders/place", _typed(f"rule-{code}", **fields), env=env)

    if code is None:
        assert resp.status_code == 200, resp.text
        assert len(quotes.ctx.mutations) == 1 and _only_row().state == "WORKING"
        return
    assert resp.status_code == (503 if setup.get("snap", 0) is None else 400), resp.text
    assert resp.json()["reason_code"] == code
    assert quotes.ctx.mutations == []
    row = _only_row()
    assert row.state == "REJECTED" and row.reason_code == code  # the reservation releases its cover


@pytest.mark.parametrize(
    "fields, codes",
    [
        ({**STP_LMT, "auxPrice": 500.2, "limitPrice": 500.2}, ["TRIGGER_WOULD_FIRE"]),
        ({**STP_LMT, "limitPrice": 510.53}, ["LIMIT_OUT_OF_BAND"]),
        ({**STP_LMT, "auxPrice": 500.2, "limitPrice": 501}, ["TRIGGER_WOULD_FIRE", "LIMIT_OUT_OF_BAND"]),
        ({**LIT, "auxPrice": 500.2, "limitPrice": 500.2}, ["TRIGGER_WOULD_FIRE"]),
    ],
)
def test_an_override_accepts_a_limit_trigger_order_and_audits_the_intent(quotes, fields, codes):
    resp = _post(quotes, "/orders/place", _typed("ack", acknowledge_limit_override=True, **fields), env="live")

    assert resp.status_code == 200, resp.text
    sid = resp.json()["submission_id"]
    [detail] = _events(sid, "PREFLIGHT_ACK_LIMIT")
    assert detail["reason_codes"] == codes and detail["limit_price"] == fields["limitPrice"]
    assert detail["futu_intent"] == _row(sid).legs["futu_intent"]
    assert detail["futu_intent"]["order_type"] == fields["orderType"]
    assert detail["futu_intent"]["aux_price"] == orders_store.canonical_decimal(Decimal(str(fields["auxPrice"])))


@pytest.mark.parametrize(
    "env, fields, setup, code",
    [
        ("live", {**STP, "auxPrice": 500.3}, {}, "TRIGGER_WOULD_FIRE"),  # an immediate market order
        ("live", {**MIT, "auxPrice": 500.1}, {}, "TRIGGER_WOULD_FIRE"),
        ("paper", MKT, {"snap": {"bid": Decimal(495), "ask": Decimal(505)}}, "MKT_SPREAD_TOO_WIDE"),
        ("live", {**TRAIL_LIMIT, "trailSpread": 26}, {}, "FUTU_INVALID_ORDER"),
    ],
)
def test_an_override_never_accepts_the_other_rules(quotes, env, fields, setup, code):
    if "snap" in setup:
        quotes.snap = {**quotes.snap, **setup["snap"]}

    resp = _post(quotes, "/orders/place", _typed("ack-no", acknowledge_limit_override=True, **fields), env=env)

    assert resp.status_code == 400 and resp.json()["reason_code"] == code, resp.text
    assert quotes.ctx.mutations == [] and _only_row().state == "REJECTED"


def test_a_futu_lmt_override_audits_the_intent(quotes):
    """A FUTU LMT keeps the existing band and override; its audit also
    carries the placement intent."""
    resp = _post(
        quotes, "/orders/place", _branch._stock(cid="lmt-ack", limitPrice=501, acknowledge_limit_override=True)
    )

    assert resp.status_code == 200, resp.text
    [detail] = _events(resp.json()["submission_id"], "PREFLIGHT_ACK_LIMIT")
    assert detail["reason_codes"] == ["LIMIT_OUT_OF_BAND"] and detail["limit_price"] == 501
    assert detail["futu_intent"]["order_type"] == "LMT"


# -- Routes: modify ---------------------------------------------------------------

MODIFY_TYPES = "LMT,MKT,STP,MIT,STP LMT,LIT,TRAIL,TRAIL LIMIT"


def _place(quotes, fields, env="live", cid="mod"):
    resp = _post(quotes, "/orders/place", _typed(cid, **fields), env=env)
    assert resp.status_code == 200, resp.text
    return resp.json()["submission_id"], resp.json()["brokerOrderId"]


def _modify(quotes, oid, env="live", seq=1, **fields):
    return _post(quotes, "/orders/modify", {"brokerOrderId": oid, "modifySequence": seq, **fields}, env=env)


LMT = {"orderType": "LMT", "limitPrice": 500.2}


@pytest.mark.parametrize(
    "env, fields, change, app, code",
    [
        # A changed field the request names.
        ("live", LMT, {"newPrice": 500.33}, {}, "LIMIT_OUT_OF_BAND"),
        ("paper", MKT, {"newQuantity": 50}, {}, "MKT_NOTIONAL_CAP"),
        ("live", STP, {"newAuxPrice": 500.3}, {}, "TRIGGER_WOULD_FIRE"),  # aux-only bypass attempt
        ("live", MIT, {"newAuxPrice": 500.1}, {}, "TRIGGER_WOULD_FIRE"),
        ("live", STP_LMT, {"newAuxPrice": 500.2}, {}, "TRIGGER_WOULD_FIRE"),  # no override on modify
        ("live", STP_LMT, {"newPrice": 510.53}, {}, "LIMIT_OUT_OF_BAND"),
        ("live", LIT, {"newAuxPrice": 490.4}, {}, "LIMIT_OUT_OF_BAND"),  # limit 490.52 > the new trigger
        ("live", TRAIL_LIMIT, {"newTrailValue": 100.05}, {}, "FUTU_INVALID_ORDER"),  # trail-only bypass
        ("live", TRAIL_LIMIT, {"newTrailSpread": 25.02}, {}, "FUTU_INVALID_ORDER"),
        # An omitted field changed in the Futu app: the full outgoing terms are judged.
        ("live", STP, {"newQuantity": 3}, {"aux_price": 500.0}, "TRIGGER_WOULD_FIRE"),
        ("live", STP_LMT, {"newQuantity": 3}, {"aux_price": 500.0, "price": 500.01}, "TRIGGER_WOULD_FIRE"),
        ("live", LIT, {"newQuantity": 3}, {"price": 491.0}, "LIMIT_OUT_OF_BAND"),
        ("live", TRAIL_LIMIT, {"newQuantity": 3}, {"trail_spread": 30.0}, "FUTU_INVALID_ORDER"),
        ("live", LMT, {"newQuantity": 3}, {"price": 501.0}, "LIMIT_OUT_OF_BAND"),
    ],
)
def test_a_modify_whose_full_terms_fail_the_quote_rule_sends_nothing(
    quotes, monkeypatch, env, fields, change, app, code
):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", MODIFY_TYPES)
    sid, oid = _place(quotes, fields, env=env)
    quotes.ctx.book[oid].update(app)
    before = _row(sid)

    mod = _modify(quotes, oid, env=env, acknowledge_limit_override=True, **change)

    assert mod.status_code == 400 and mod.json()["reason_code"] == code, mod.text
    assert mod.json()["detail"]["applied_sequence"] == 1
    assert _modifies(quotes) == []
    after = _row(sid)  # the reservation is intact: never the placement's REJECTED wrapper
    assert (after.state, after.quantity, after.limit_price) == ("WORKING", before.quantity, before.limit_price)
    assert after.legs.get("pending_amend_id") is None and after.legs.get("pending_amend_qty") is None
    assert after.legs["futu_terms"] == before.legs["futu_terms"]
    cancel = _post(quotes, "/orders/cancel", {"brokerOrderId": oid}, env=env)
    assert cancel.status_code == 200 and cancel.json()["confirmed"] is True  # cancel needs no quote


def test_a_modify_with_ib_quotes_down_sends_nothing_and_keeps_the_sequence(quotes, monkeypatch):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", MODIFY_TYPES)
    sid, oid = _place(quotes, STP)
    quotes.snap = None

    mod = _modify(quotes, oid, newAuxPrice=511)

    assert mod.status_code == 503 and mod.json()["reason_code"] == "QUOTE_UNAVAILABLE", mod.text
    assert _modifies(quotes) == [] and _row(sid).modify_sequence == 0
    assert quotes.fetches[-1] == {"type": "stock", "symbol": "SPY", "expiry": None, "strike": None, "right": None}


@pytest.mark.parametrize(
    "fields, change, sent",
    [
        (LMT, {"newPrice": 500.25}, {"price": 500.25}),
        (STP, {"newAuxPrice": 511}, {"aux_price": 511.0}),
        (MIT, {"newAuxPrice": 490}, {"aux_price": 490.0}),
        (STP_LMT, {"newAuxPrice": 511, "newPrice": 511.02}, {"aux_price": 511.0, "price": 511.02}),
        (LIT, {"newAuxPrice": 490.4, "newPrice": 490.4}, {"aux_price": 490.4, "price": 490.4}),
        (TRAIL_LIMIT, {"newTrailValue": 3.25, "newTrailSpread": 0.1}, {"trail_value": 3.25, "trail_spread": 0.1}),
    ],
)
def test_a_passing_modify_is_sent_confirmed_and_moves_only_the_terms(quotes, monkeypatch, fields, change, sent):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", MODIFY_TYPES)
    sid, oid = _place(quotes, fields)
    intent = _row(sid).legs["futu_intent"]

    mod = _modify(quotes, oid, **change)

    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    [kw] = _modifies(quotes)
    assert {k: kw[k] for k in sent} == sent
    row = _row(sid)
    assert row.legs["futu_intent"] == intent  # never touched by settle_modify
    terms = row.legs["futu_terms"]
    for key in ("aux_price", "trail_value", "trail_spread"):
        if key in sent:
            assert terms[key] == orders_store.canonical_decimal(Decimal(str(sent[key])))
    if "price" in sent:
        assert row.limit_price == Decimal(str(sent["price"]))


def test_a_trailing_order_never_compares_the_derived_broker_price(quotes, monkeypatch):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", MODIFY_TYPES)
    sid, oid = _place(quotes, TRAIL_LIMIT)
    real = quotes.ctx.modify_order

    def _moving_stop(**kw):
        out = real(**kw)
        quotes.ctx.book[oid]["price"] = 497.77  # Futu's moving trigger / limit
        return out

    monkeypatch.setattr(quotes.ctx, "modify_order", _moving_stop)

    mod = _modify(quotes, oid, newTrailValue=3)

    assert mod.status_code == 200 and mod.json()["confirmed"] is True, mod.text
    assert mod.json()["confirmedPrice"] is None and _row(sid).legs["futu_terms"]["trail_value"] == "3"


@pytest.mark.parametrize(
    "fields, change, dropped",
    [
        (LMT, {"newPrice": 500.25}, "price"),
        (LMT, {"newQuantity": 3}, "qty"),
        (STP, {"newAuxPrice": 511}, "aux_price"),
        (STP_LMT, {"newAuxPrice": 510.51}, "aux_price"),
        (STP_LMT, {"newPrice": 510.51}, "price"),
        (TRAIL_LIMIT, {"newTrailValue": 3.25}, "trail_value"),
        (TRAIL_LIMIT, {"newTrailSpread": 0.1}, "trail_spread"),
    ],
)
def test_a_sent_field_futu_does_not_report_holds_modify_pending(quotes, monkeypatch, fields, change, dropped):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", MODIFY_TYPES)
    sid, oid = _place(quotes, fields)
    before = _row(sid)
    real = quotes.ctx.modify_order

    def _one_field_lags(**kw):
        old = quotes.ctx.book[oid][dropped]
        out = real(**kw)
        quotes.ctx.book[oid][dropped] = old  # the fresh read still shows the old value
        return out

    monkeypatch.setattr(quotes.ctx, "modify_order", _one_field_lags)

    mod = _modify(quotes, oid, **change)

    assert mod.status_code == 200 and mod.json()["confirmed"] is False, mod.text
    assert mod.json()["confirmedPrice"] is None and mod.json()["confirmedQuantity"] is None
    row = _row(sid)
    assert row.legs["pending_amend_id"] is not None  # the larger reservation stays
    assert row.legs["futu_terms"] == before.legs["futu_terms"] and row.limit_price == before.limit_price
    again = _modify(quotes, oid, seq=2, newQuantity=4)
    assert again.status_code == 409 and again.json()["reason_code"] == "MODIFY_PENDING", again.text
    assert len(_modifies(quotes)) == 1


@pytest.mark.parametrize(
    "fields, change",
    [
        (STP, {"newTrailValue": 2}),
        (STP, {"newTrailSpread": 0.1}),
        (TRAIL_LIMIT, {"newAuxPrice": 511}),
        (MKT, {"newAuxPrice": 511}),
        (LMT, {"newTrailSpread": 0.1}),
    ],
)
def test_a_new_field_the_type_lacks_is_refused_before_the_sequence(quotes, monkeypatch, fields, change):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", MODIFY_TYPES)
    env = "paper" if fields is MKT else "live"
    sid, oid = _place(quotes, fields, env=env)

    mod = _modify(quotes, oid, env=env, **change)

    assert mod.status_code == 400 and mod.json()["reason_code"] == "INVALID_ORDER_BODY", mod.text
    assert _modifies(quotes) == [] and _row(sid).modify_sequence == 0


@pytest.mark.parametrize(
    "change, code",
    [
        ({"newAuxPrice": 511.005}, "FUTU_PRICE_PRECISION"),
        ({"newAuxPrice": 0}, "FUTU_INVALID_ORDER"),
        ({"newAuxPrice": "x"}, "INVALID_ORDER_BODY"),
        ({}, "INVALID_ORDER_BODY"),  # no new field
    ],
)
def test_a_bad_new_trigger_is_refused_before_the_sequence(quotes, monkeypatch, change, code):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", MODIFY_TYPES)
    sid, oid = _place(quotes, STP)

    mod = _modify(quotes, oid, **change)

    assert mod.status_code == 400 and mod.json()["reason_code"] == code, mod.text
    assert _modifies(quotes) == [] and _row(sid).modify_sequence == 0


@pytest.mark.parametrize(
    "change, code",
    [({"newTrailValue": 3.123}, "FUTU_PRICE_PRECISION"), ({"newTrailSpread": 0.0005}, "FUTU_PRICE_PRECISION")],
)
def test_a_bad_new_trail_term_is_refused_before_the_sequence(quotes, monkeypatch, change, code):
    monkeypatch.setenv("XENON_FUTU_MODIFY_TYPES", MODIFY_TYPES)
    sid, oid = _place(quotes, {**TRAIL_LIMIT, "trailType": "RATIO", "trailValue": 1.5})

    mod = _modify(quotes, oid, **change)

    assert mod.status_code == 400 and mod.json()["reason_code"] == code, mod.text
    assert _modifies(quotes) == [] and _row(sid).modify_sequence == 0
