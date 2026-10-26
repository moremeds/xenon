"""X5a task 1: Futu allowed tuples, order types and precision
(`execution/futu_orders.py`), over an autospec'd `OpenSecTradeContext` (no
OpenD, no network). Plan § 1, § 2, § 5; Astra A3, A4, A7, R2-3, R2-4, R3-1.
Ids, prices and trail values are made-up test values, not market data."""

from __future__ import annotations

import itertools
import logging
from decimal import Decimal

import pytest

from scripts.tests.test_futu_orders_adapter import ACC, REAL_ENV, FakeCtx, _frame, _opt, _stk
from xenon.execution import futu_orders as fo
from xenon.execution.futu_orders import FutuOrderRefused

V016_LIVE = {
    ("STK", "LMT", "DAY", "RTH"),
    ("STK", "LMT", "GTC", "RTH"),
    ("OPT", "LMT", "DAY", "RTH"),
    ("OPT", "LMT", "GTC", "RTH"),
}
PAPER_DEFAULT = {("STK", "LMT", "DAY", "RTH"), ("OPT", "LMT", "DAY", "RTH"), ("STK", "MKT", "DAY", "RTH")}
ALL_TUPLES = list(itertools.product(fo.SEC_TYPES, fo.ORDER_TYPE_TO_FUTU, fo.TIFS, fo.SESSIONS))
EVERY_TYPE = {"XENON_FUTU_MODIFY_TYPES": ",".join(fo.ORDER_TYPE_TO_FUTU)}


def _env(live=None, paper=None, **extra):
    env = dict(REAL_ENV)
    if live is not None:
        env[fo.TUPLES_ENV["live"]] = live
    if paper is not None:
        env[fo.TUPLES_ENV["paper"]] = paper
    env.update(extra)
    return env


def _tok(t):
    return ":".join(t)


def _place(ctx, spec, env="live", environ=None):
    return fo.place_single(
        ctx, spec, submission_id="sub-1", account_env=env, acc_id=ACC, mark_sent=ctx.mark, environ=environ or REAL_ENV
    )


def _modify(ctx, spec, env="live", environ=None):
    return fo.modify(
        ctx, spec, order_id="8801", account_env=env, acc_id=ACC, mark_sent=ctx.mark, environ=environ or REAL_ENV
    )


def _cancel(ctx, env="live", environ=None):
    return fo.cancel(ctx, order_id="8801", account_env=env, acc_id=ACC, mark_sent=ctx.mark, environ=environ or REAL_ENV)


def _sent(ctx, name):
    return [kw for n, _, kw in ctx.log if n == name]


# ---------------------------------------------------------------------------
# Static set, defaults, allowlists
# ---------------------------------------------------------------------------


def test_static_set_matches_the_contract():
    live, paper = fo.STATIC_TUPLES["live"], fo.STATIC_TUPLES["paper"]
    assert paper == PAPER_DEFAULT  # SIMULATE: limit + market, DAY, RTH
    assert {t for t in live if t[0] == "OPT"} == {
        ("OPT", ty, tif, "RTH") for ty in ("LMT", "STP LMT", "LIT") for tif in fo.TIFS
    }
    assert {t for t in live if t[:2] == ("STK", "LMT")} == {("STK", "LMT", f, s) for f in fo.TIFS for s in fo.SESSIONS}
    assert {t for t in live if t[:2] == ("STK", "MKT")} == {("STK", "MKT", "DAY", "RTH")}
    for ty in ("STP", "STP LMT", "MIT", "LIT", "TRAIL", "TRAIL LIMIT"):
        assert {t for t in live if t[:2] == ("STK", ty)} == {("STK", ty, f, "RTH") for f in fo.TIFS}


def test_unset_allowlists_keep_the_v016_surface():
    assert fo.allowed_tuples("live", {}) == V016_LIVE
    assert fo.allowed_tuples("paper", {}) == PAPER_DEFAULT
    # blank (e.g. compose `VAR=`) is the default too
    assert fo.allowed_tuples("live", _env(live="  ")) == V016_LIVE
    assert fo.modify_types({}) == {"LMT"}
    assert fo.allowed_tuples("nowhere", {}) == frozenset()


@pytest.mark.parametrize("env", ["live", "paper"])
@pytest.mark.parametrize("tup", ALL_TUPLES, ids=_tok)
def test_every_tuple_outside_the_default_is_refused_with_its_dimension(env, tup):
    sec, ty, tif, session = tup
    got = fo.allowed(env, sec, ty, tif, session, environ={})
    default = V016_LIVE if env == "live" else PAPER_DEFAULT
    if tup in default:
        assert got is None
        return
    code, detail = got
    if not any(t[:2] == (sec, ty) for t in default):
        assert code == fo.UNSUPPORTED_ORDER_TYPE and ty in detail
    elif not any(t[:3] == (sec, ty, tif) for t in default):
        assert code == fo.TIF_UNSUPPORTED and tif in detail
    else:
        assert code == fo.SESSION_UNSUPPORTED and session in detail


@pytest.mark.parametrize(
    "env, tup, code",
    [
        ("paper", ("STK", "LMT", "GTC", "RTH"), fo.TIF_UNSUPPORTED),  # paper GTC withdrawn
        ("paper", ("OPT", "MKT", "DAY", "RTH"), fo.UNSUPPORTED_ORDER_TYPE),
        ("paper", ("STK", "MKT", "DAY", "ETH"), fo.SESSION_UNSUPPORTED),
        ("live", ("STK", "MKT", "DAY", "RTH"), fo.UNSUPPORTED_ORDER_TYPE),  # live MKT not by default
        ("live", ("STK", "LMT", "DAY", "OVERNIGHT"), fo.SESSION_UNSUPPORTED),
        ("live", ("OPT", "TRAIL", "DAY", "RTH"), fo.UNSUPPORTED_ORDER_TYPE),
        ("live", ("STK", "LMT", "GTD", "RTH"), fo.TIF_UNSUPPORTED),  # not a TIF at all
        ("live", ("STK", "LMT", "DAY", "PRE"), fo.SESSION_UNSUPPORTED),  # not a session at all
        ("live", ("STK", "ICEBERG", "DAY", "RTH"), fo.UNSUPPORTED_ORDER_TYPE),
        ("live", ("FUT", "LMT", "DAY", "RTH"), fo.INVALID_ORDER),
    ],
)
def test_named_refusals(env, tup, code):
    assert fo.allowed(env, *tup, environ={})[0] == code


@pytest.mark.parametrize("env", ["live", "paper"])
def test_a_single_added_tuple_enables_only_itself(env):
    default = V016_LIVE if env == "live" else PAPER_DEFAULT
    extra = sorted(fo.STATIC_TUPLES[env] - default)
    if env == "paper":
        assert extra == []  # nothing to add on paper: the default is the whole static set
    for t in extra:
        raw = ",".join(_tok(x) for x in sorted(default | {t}))
        environ = _env(**{env: raw})
        assert fo.allowed_tuples(env, environ) - default == {t}
        assert fo.allowed(env, *t, environ=environ) is None
        others = [x for x in ALL_TUPLES if x != t and x not in default]
        assert all(fo.allowed(env, *x, environ=environ) is not None for x in others)


def test_enabling_a_stock_day_tuple_does_not_enable_gtc_option_or_other_session():
    environ = _env(live=",".join(_tok(x) for x in V016_LIVE) + ",STK:STP LMT:DAY:RTH")
    assert fo.allowed("live", "STK", "STP LMT", "DAY", "RTH", environ=environ) is None
    assert fo.allowed("live", "STK", "STP LMT", "GTC", "RTH", environ=environ)[0] == fo.TIF_UNSUPPORTED
    assert fo.allowed("live", "OPT", "STP LMT", "DAY", "RTH", environ=environ)[0] == fo.UNSUPPORTED_ORDER_TYPE
    assert fo.allowed("live", "STK", "STP LMT", "DAY", "ETH", environ=environ)[0] == fo.SESSION_UNSUPPORTED
    assert fo.allowed("live", "STK", "STP", "DAY", "RTH", environ=environ)[0] == fo.UNSUPPORTED_ORDER_TYPE


def test_allowlist_entries_are_exact_and_whitespace_tolerant():
    environ = _env(live=" stk : stp  lmt : day : rth ")
    assert fo.allowed_tuples("live", environ) == {("STK", "STP LMT", "DAY", "RTH")}  # replaces the default


def test_a_tuple_outside_the_static_set_is_ignored_with_an_error_log(caplog):
    environ = _env(live="OPT:MKT:DAY:RTH,STK:LMT:DAY:RTH")
    with caplog.at_level(logging.ERROR):
        assert fo.allowed_tuples("live", environ) == {("STK", "LMT", "DAY", "RTH")}
    assert fo.allowed("live", "OPT", "MKT", "DAY", "RTH", environ=environ)[0] == fo.UNSUPPORTED_ORDER_TYPE
    assert "XENON_FUTU_TUPLES_LIVE" in caplog.text


@pytest.mark.parametrize("raw", ["STK:LMT:DAY", "STK:LMT:DAY:RTH,STK:*:DAY:RTH", "STK:LMT:DAY:RTH:X", "garbage"])
def test_a_malformed_allowlist_turns_every_tuple_off(raw, caplog):
    with caplog.at_level(logging.ERROR):
        assert fo.allowed_tuples("live", _env(live=raw)) == frozenset()
    assert fo.allowed("live", "STK", "LMT", "DAY", "RTH", environ=_env(live=raw))[0] == fo.UNSUPPORTED_ORDER_TYPE


def test_modify_allowlist_is_per_type_and_fails_closed():
    environ = _env(live="STK:LMT:DAY:RTH,STK:STP LMT:DAY:RTH")
    assert fo.allowed("live", "STK", "LMT", "DAY", "RTH", op="modify", environ=environ) is None
    code, _ = fo.allowed("live", "STK", "STP LMT", "DAY", "RTH", op="modify", environ=environ)
    assert code == fo.NOT_MODIFIABLE
    assert fo.allowed("live", "STK", "STP LMT", "DAY", "RTH", environ=environ) is None  # place unaffected
    environ["XENON_FUTU_MODIFY_TYPES"] = "LMT, stp lmt"
    assert fo.allowed("live", "STK", "STP LMT", "DAY", "RTH", op="modify", environ=environ) is None
    environ["XENON_FUTU_MODIFY_TYPES"] = "LMT,BOGUS"
    assert fo.modify_types(environ) == frozenset()
    assert fo.allowed("live", "STK", "LMT", "DAY", "RTH", op="modify", environ=environ)[0] == fo.NOT_MODIFIABLE
    # the tuple check comes first: an order outside the allowlist names its dimension
    assert fo.allowed("live", "STK", "LMT", "DAY", "ETH", op="modify", environ={})[0] == fo.SESSION_UNSUPPORTED


# ---------------------------------------------------------------------------
# Upgrade fixture (R3-1): pre-existing GTC LMT, unset variables
# ---------------------------------------------------------------------------


def test_upgrade_live_gtc_lmt_keeps_place_modify_cancel():
    ctx = FakeCtx()
    gtc = _stk(tif="GTC")
    assert _place(ctx, gtc, "live", REAL_ENV).outcome == "accepted"
    assert _modify(ctx, gtc, "live", REAL_ENV).outcome == "sent"
    assert _cancel(ctx, "live", REAL_ENV).outcome == "sent"
    assert [kw["time_in_force"] for kw in _sent(ctx, "place_order")] == ["GTC"]
    assert [kw["modify_order_op"] for kw in _sent(ctx, "modify_order")] == ["NORMAL", "CANCEL"]


def test_upgrade_paper_gtc_lmt_refuses_place_and_modify_keeps_cancel():
    ctx = FakeCtx()
    gtc = _stk(tif="GTC")
    for call in (lambda: _place(ctx, gtc, "paper", {}), lambda: _modify(ctx, gtc, "paper", {})):
        with pytest.raises(FutuOrderRefused) as exc:
            call()
        assert exc.value.reason_code == fo.TIF_UNSUPPORTED and exc.value.http_status == 400
    assert ctx.log == []  # nothing sent, no send mark
    assert _cancel(ctx, "paper", {}).outcome == "sent"
    assert ctx.names() == ["mark", "modify_order"]


# ---------------------------------------------------------------------------
# SDK keywords per type and per operation (R2-3), autospec'd context
# ---------------------------------------------------------------------------

ALL_LIVE = ",".join(_tok(t) for t in sorted(fo.STATIC_TUPLES["live"]))
LIVE_ALL = _env(live=ALL_LIVE, **EVERY_TYPE)

TYPE_SPECS = {
    "LMT": (dict(limit_price=Decimal("1.25")), 1.25, {}),
    "MKT": (dict(limit_price=None), 0.0, {}),
    "STP": (dict(limit_price=None, aux_price=Decimal("1.40")), 0.0, {"aux_price": 1.40}),
    "STP LMT": (
        dict(limit_price=Decimal("1.42"), aux_price=Decimal("1.40")),
        1.42,
        {"aux_price": 1.40},
    ),
    "MIT": (dict(limit_price=None, aux_price=Decimal("1.10")), 0.0, {"aux_price": 1.10}),
    "LIT": (
        dict(limit_price=Decimal("1.11"), aux_price=Decimal("1.10")),
        1.11,
        {"aux_price": 1.10},
    ),
    "TRAIL": (
        dict(limit_price=None, trail_type="RATIO", trail_value=Decimal("2.5")),
        0.0,
        {"trail_type": "RATIO", "trail_value": 2.5},
    ),
    "TRAIL LIMIT": (
        dict(limit_price=None, trail_type="AMOUNT", trail_value=Decimal("0.1234"), trail_spread=Decimal("0.015")),
        0.0,
        {"trail_type": "AMOUNT", "trail_value": 0.1234, "trail_spread": 0.015},
    ),
}


@pytest.mark.parametrize("order_type", list(TYPE_SPECS))
def test_place_sends_the_futu_enum_price_or_placeholder_and_only_the_types_terms(order_type):
    fields, price, terms = TYPE_SPECS[order_type]
    ctx = FakeCtx()

    out = _place(ctx, _stk(order_type=order_type, **fields), "live", LIVE_ALL)

    assert out.outcome == "accepted", out  # autospec bound every keyword
    assert ctx.names() == ["unlock_trade", "mark", "place_order"]
    [kw] = _sent(ctx, "place_order")
    assert kw == {
        "price": price,
        "qty": 10,
        "code": "US.SPY",
        "trd_side": "BUY",
        "order_type": fo.ORDER_TYPE_TO_FUTU[order_type],
        "adjust_limit": 0,
        "trd_env": "REAL",
        "acc_id": int(ACC),
        "remark": "xo:sub-1",
        "time_in_force": "DAY",
        **terms,
    }
    assert "session" not in kw  # RTH: SDK default Session.NONE, the v0.16 wire


@pytest.mark.parametrize("order_type", list(TYPE_SPECS))
def test_modify_sends_the_types_terms_without_session_or_order_type(order_type):
    fields, price, terms = TYPE_SPECS[order_type]
    ctx = FakeCtx()

    out = _modify(ctx, _stk(order_type=order_type, quantity=3, **fields), "live", LIVE_ALL)

    assert out.outcome == "sent", out
    [kw] = _sent(ctx, "modify_order")
    assert kw == {
        "modify_order_op": "NORMAL",
        "order_id": "8801",
        "qty": 3,
        "price": price,
        "adjust_limit": 0,
        "trd_env": "REAL",
        "acc_id": int(ACC),
        **terms,
    }


def test_cancel_sends_only_its_existing_arguments():
    ctx = FakeCtx()
    _cancel(ctx, "live", LIVE_ALL)
    [kw] = _sent(ctx, "modify_order")
    assert kw == {
        "modify_order_op": "CANCEL",
        "order_id": "8801",
        "qty": 0,
        "price": 0.0,
        "adjust_limit": 0,
        "trd_env": "REAL",
        "acc_id": int(ACC),
    }


@pytest.mark.parametrize("session", ["ETH", "ALL", "OVERNIGHT"])
def test_place_sends_a_non_rth_session_and_modify_never_does(session):
    ctx = FakeCtx()
    spec = _stk(session=session)
    _place(ctx, spec, "live", LIVE_ALL)
    _modify(ctx, spec, "live", LIVE_ALL)
    [place] = _sent(ctx, "place_order")
    [mod] = _sent(ctx, "modify_order")
    assert place["session"] == session and "session" not in mod


def test_autospec_rejects_a_keyword_the_sdk_does_not_have():
    """The R2-3 regression (session on modify) fails against the real signature."""
    ctx = FakeCtx()
    with pytest.raises(TypeError):
        ctx.modify_order(modify_order_op="NORMAL", order_id="1", qty=1, price=1.0, session="RTH")
    with pytest.raises(TypeError):
        ctx.place_order(price=1.0, qty=1, code="US.SPY", trd_side="BUY", stop_price=1.0)


def test_option_stop_limit_and_lit_send_aux_with_their_limit():
    ctx = FakeCtx()
    spec = _opt(order_type="STP LMT", limit_price=Decimal("2.10"), aux_price=Decimal("2.15"))
    _place(ctx, spec, "live", LIVE_ALL)
    [kw] = _sent(ctx, "place_order")
    assert (kw["order_type"], kw["price"], kw["aux_price"]) == ("STOP_LIMIT", 2.10, 2.15)


# ---------------------------------------------------------------------------
# Fields per type (plan § 1, A3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "order_type, fields",
    [
        ("LMT", dict(limit_price=None)),
        ("LMT", dict(aux_price=Decimal("1.30"))),
        ("MKT", dict(limit_price=Decimal("1.25"))),
        ("STP", dict(limit_price=None)),  # no trigger
        ("STP", dict(limit_price=Decimal("1.25"), aux_price=Decimal("1.30"))),  # extra limit
        ("STP LMT", dict(aux_price=None)),
        ("LIT", dict(limit_price=None, aux_price=Decimal("1.10"))),
        ("TRAIL", dict(limit_price=None, trail_type="RATIO")),  # no value
        ("TRAIL", dict(limit_price=None, trail_value=Decimal("1"))),  # no type
        ("TRAIL", dict(limit_price=None, trail_type="RATIO", trail_value=Decimal("1"), trail_spread=Decimal("0"))),
        # TRAIL LIMIT takes no fixed limit: Futu derives it from trigger + spread (A3)
        (
            "TRAIL LIMIT",
            dict(limit_price=Decimal("1.25"), trail_type="RATIO", trail_value=Decimal("1"), trail_spread=Decimal("0")),
        ),
        ("TRAIL LIMIT", dict(limit_price=None, trail_type="RATIO", trail_value=Decimal("1"))),  # no spread
        ("TRAIL", dict(limit_price=None, trail_type="PERCENT", trail_value=Decimal("1"))),
    ],
)
def test_a_missing_or_extra_field_is_refused_before_any_call(order_type, fields):
    ctx = FakeCtx()
    with pytest.raises(FutuOrderRefused) as exc:
        _place(ctx, _stk(order_type=order_type, **fields), "live", LIVE_ALL)
    assert exc.value.reason_code == fo.INVALID_ORDER and exc.value.http_status == 400
    assert ctx.log == []


# ---------------------------------------------------------------------------
# Precision boundaries (A7): refused, never rounded
# ---------------------------------------------------------------------------


def _trail(value, kind="RATIO", spread=None, sec="STK"):
    order_type = "TRAIL LIMIT" if spread is not None else "TRAIL"
    base = _stk if sec == "STK" else _opt
    return base(order_type=order_type, limit_price=None, trail_type=kind, trail_value=value, trail_spread=spread)


@pytest.mark.parametrize(
    "value, code",
    [
        (Decimal("0.004"), fo.PRICE_PRECISION),  # would round to 0.00: refused, not sent as zero
        (Decimal("0.005"), fo.PRICE_PRECISION),
        (Decimal("0.01"), None),
        (Decimal("1.234"), fo.PRICE_PRECISION),  # Futu keeps 2 dp: 1.23 would never confirm
        (Decimal("1.230"), None),  # same value as 1.23
        (Decimal("20"), None),
        (Decimal("20.01"), fo.INVALID_ORDER),
        (Decimal("0"), fo.INVALID_ORDER),
        (Decimal("-1"), fo.INVALID_ORDER),
        ("nan", fo.INVALID_ORDER),
        ("inf", fo.INVALID_ORDER),
    ],
)
def test_trail_ratio_boundaries(value, code):
    ctx = FakeCtx()
    spec = _trail(value)
    if code is None:
        _place(ctx, spec, "live", LIVE_ALL)
        [kw] = _sent(ctx, "place_order")
        assert kw["trail_value"] == float(Decimal(value)) and kw["trail_type"] == "RATIO"
        return
    with pytest.raises(FutuOrderRefused) as exc:
        _place(ctx, spec, "live", LIVE_ALL)
    assert exc.value.reason_code == code and ctx.log == []


@pytest.mark.parametrize(
    "sec, value, ok",
    [
        ("STK", Decimal("0.1234"), True),  # stock amount: 4 dp
        ("STK", Decimal("0.12345"), False),
        ("STK", Decimal("0.00001"), False),  # rounds to zero at 4 dp
        ("STK", Decimal("0.0001"), True),
        ("OPT", Decimal("0.12"), True),  # option amount: 2 dp
        ("OPT", Decimal("0.123"), False),
        ("OPT", Decimal("0.001"), False),  # rounds to zero at 2 dp
    ],
)
def test_trail_amount_decimals_per_security(sec, value, ok):
    spec = _trail(value, kind="AMOUNT", sec=sec)
    if ok:
        assert fo.checked_trail_value(spec, value) == value
    else:
        with pytest.raises(FutuOrderRefused) as exc:
            fo.checked_trail_value(spec, value)
        assert exc.value.reason_code == fo.PRICE_PRECISION
    # an option trailing order is never placeable (static set), whatever its decimals
    if sec == "OPT":
        with pytest.raises(FutuOrderRefused) as exc:
            _place(FakeCtx(), spec, "live", LIVE_ALL)
        assert exc.value.reason_code == fo.UNSUPPORTED_ORDER_TYPE


@pytest.mark.parametrize(
    "spread, code",
    [
        (Decimal("0"), None),  # zero spread: limit = trigger
        (Decimal("0.000"), None),
        (Decimal("0.125"), None),
        (Decimal("0.1255"), fo.PRICE_PRECISION),
        (Decimal("0.0004"), fo.PRICE_PRECISION),  # rounds to zero at 3 dp
        (Decimal("-0.001"), fo.INVALID_ORDER),
    ],
)
def test_trail_spread_boundaries(spread, code):
    ctx = FakeCtx()
    spec = _trail(Decimal("1"), spread=spread)
    if code is None:
        _place(ctx, spec, "live", LIVE_ALL)
        [kw] = _sent(ctx, "place_order")
        assert kw["trail_spread"] == float(spread) and kw["price"] == fo.PLACEHOLDER_PRICE
        return
    with pytest.raises(FutuOrderRefused) as exc:
        _place(ctx, spec, "live", LIVE_ALL)
    assert exc.value.reason_code == code and ctx.log == []


@pytest.mark.parametrize(
    "spec, ok",
    [
        (_stk(order_type="STP", limit_price=None, aux_price=Decimal("1.25")), True),
        (_stk(order_type="STP", limit_price=None, aux_price=Decimal("1.255")), False),
        (_stk(order_type="STP", limit_price=None, aux_price=Decimal("0.5001")), True),  # < $1: 0.0001
        (_opt(order_type="LIT", limit_price=Decimal("2.15"), aux_price=Decimal("2.155")), False),
        (_opt(order_type="LIT", limit_price=Decimal("2.155"), aux_price=Decimal("2.15")), False),
    ],
)
def test_trigger_price_is_on_the_price_grid(spec, ok):
    ctx = FakeCtx()
    if ok:
        _place(ctx, spec, "live", LIVE_ALL)
        assert _sent(ctx, "place_order")[0]["aux_price"] == float(spec.aux_price)
    else:
        with pytest.raises(FutuOrderRefused) as exc:
            _place(ctx, spec, "live", LIVE_ALL)
        assert exc.value.reason_code == fo.PRICE_PRECISION and ctx.log == []


# ---------------------------------------------------------------------------
# Fresh evidence carries type / aux / trail / TIF / session
# ---------------------------------------------------------------------------


def test_fresh_read_evidence_carries_type_and_terms():
    ctx = FakeCtx()
    ctx.replies["order_list_query"] = (
        0,
        _frame(
            [
                {
                    "order_id": "8801",
                    "order_status": "SUBMITTED",
                    "qty": 5.0,
                    "price": 0.0,
                    "order_type": "TRAILING_STOP_LIMIT",
                    "time_in_force": "GTC",
                    "session": "RTH",
                    "aux_price": "N/A",
                    "trail_type": "AMOUNT",
                    "trail_value": 0.25,
                    "trail_spread": 0.015,
                }
            ]
        ),
    )

    ev = fo.query_order(ctx, order_id="8801", account_env="live", acc_id=ACC)

    assert (ev.futu_order_type, ev.order_type) == ("TRAILING_STOP_LIMIT", "TRAIL LIMIT")
    assert (ev.tif, ev.session) == ("GTC", "RTH")
    assert ev.aux_price is None  # 'N/A' is absent, never zero
    assert (ev.trail_type, ev.trail_value, ev.trail_spread) == ("AMOUNT", Decimal("0.25"), Decimal("0.015"))


def test_fresh_read_of_an_unmapped_type_keeps_the_raw_type_only():
    ctx = FakeCtx()
    ctx.replies["order_list_query"] = (
        0,
        _frame([{"order_id": "8801", "order_status": "SUBMITTED", "order_type": "TWAP", "aux_price": 1.5}]),
    )
    ev = fo.query_order(ctx, order_id="8801", account_env="live", acc_id=ACC)
    assert (ev.futu_order_type, ev.order_type, ev.aux_price) == ("TWAP", None, Decimal("1.5"))


def test_label_map_is_a_bijection_over_sdk_order_types():
    from futu.common.constant import OrderType

    assert set(fo.ORDER_TYPE_TO_FUTU.values()) <= {v for k, v in vars(OrderType).items() if k.isupper()}
    assert {fo.FUTU_TO_ORDER_TYPE[v]: v for v in fo.ORDER_TYPE_TO_FUTU.values()} == fo.ORDER_TYPE_TO_FUTU
    assert fo.LIMIT_TYPES | fo.AUX_TYPES | fo.TRAIL_TYPES | {"MKT"} == set(fo.ORDER_TYPE_TO_FUTU)
