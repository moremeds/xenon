"""X3c task 1: Futu order adapter (`execution/futu_orders.py`) over a fake
trade context. No OpenD, no network. Ids, prices and the unlock secret are
made-up test values, not market data or a real credential."""

from __future__ import annotations

import inspect
import logging
from decimal import Decimal

import pandas as pd
import pytest

from xenon.execution import futu_orders as fo
from xenon.execution.futu_orders import FutuOrderRefused, FutuOrderSpec

ACC = "281756479345015383"
SECRET = "0123456789abcdef0123456789ABCDEF"  # made-up, format-valid
REAL_ENV = {fo.PWD_MD5_ENV: SECRET}

ORDER_COLS = ["code", "order_id", "order_status", "qty", "price", "dealt_qty", "remark"]


def _frame(rows):
    return pd.DataFrame([{c: r.get(c) for c in ORDER_COLS} for r in rows], columns=ORDER_COLS)


class FakeCtx:
    """OpenSecTradeContext double; records (name, args, kwargs) and the
    position of `mark` events in one ordered log."""

    def __init__(self):
        self.log: list[tuple] = []
        self.replies: dict[str, object] = {}

    def _call(self, name, args, kwargs, default):
        self.log.append((name, args, kwargs))
        reply = self.replies.get(name, default)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def mark(self):
        self.log.append(("mark", (), {}))

    def names(self):
        return [entry[0] for entry in self.log]

    def unlock_trade(self, *args, **kwargs):
        return self._call("unlock_trade", args, kwargs, (0, None))

    def place_order(self, *args, **kwargs):
        default = (0, _frame([{"order_id": "8801", "code": kwargs.get("code"), "order_status": "SUBMITTING"}]))
        return self._call("place_order", args, kwargs, default)

    def modify_order(self, *args, **kwargs):
        return self._call(
            "modify_order", args, kwargs, (0, pd.DataFrame([{"trd_env": "SIMULATE", "order_id": "8801"}]))
        )

    def order_list_query(self, *args, **kwargs):
        return self._call("order_list_query", args, kwargs, (0, _frame([])))

    def history_order_list_query(self, *args, **kwargs):
        return self._call("history_order_list_query", args, kwargs, (0, _frame([])))


MUTATIONS = {"place_order", "modify_order"}


def _stk(**kw):
    return FutuOrderSpec(
        **{"sec_type": "STK", "symbol": "SPY", "action": "BUY", "quantity": 10, "limit_price": Decimal("1.25"), **kw}
    )


def _opt(**kw):
    base = {
        "sec_type": "OPT",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 1,
        "limit_price": Decimal("2.15"),
        "expiry": "20261218",
        "right": "C",
        "strike": Decimal("500"),
    }
    return FutuOrderSpec(**{**base, **kw})


def _place(ctx, spec, env="paper", environ=None, sid="sub-1"):
    return fo.place_single(
        ctx, spec, submission_id=sid, account_env=env, acc_id=ACC, mark_sent=ctx.mark, environ=environ or {}
    )


# ---------------------------------------------------------------------------
# Status map (contract item 4)
# ---------------------------------------------------------------------------


def test_every_sdk_order_status_maps_and_pending_or_unknown_is_unknown():
    from futu.common.constant import OrderStatus

    values = [v for k, v in vars(OrderStatus).items() if k.isupper() and isinstance(v, str)]
    assert len(values) == 17  # SDK 10.03 common/constant.py:1288-1305
    expected = {
        "N/A": "UNKNOWN",
        "UNSUBMITTED": "UNKNOWN",
        "WAITING_SUBMIT": "UNKNOWN",
        "SUBMITTING": "UNKNOWN",
        "TIMEOUT": "UNKNOWN",
        "FILL_CANCELLED": "UNKNOWN",
        "SUBMIT_FAILED": "REJECTED",
        "FAILED": "REJECTED",
        "SUBMITTED": "WORKING",
        "CANCELLING_ALL": "WORKING",
        "FILLED_PART": "PARTIALLY_FILLED",
        "CANCELLING_PART": "PARTIALLY_FILLED",
        "FILLED_ALL": "FILLED",
        "CANCELLED_PART": "CANCELLED",
        "CANCELLED_ALL": "CANCELLED",
        "DISABLED": "CANCELLED",
        "DELETED": "CANCELLED",
    }
    assert {v: fo.state_for(v) for v in values} == expected
    assert fo.state_for("SOMETHING_NEW") == fo.state_for(None) == fo.state_for("") == "UNKNOWN"


# ---------------------------------------------------------------------------
# Limit only, price precision, remark, code (contract items 2-3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order_type", ["MKT", "STP", "STP LMT", "TRAIL"])
def test_non_limit_orders_are_refused_before_any_call(order_type):
    ctx = FakeCtx()
    with pytest.raises(FutuOrderRefused) as exc:
        _place(ctx, _stk(order_type=order_type))
    assert exc.value.reason_code == fo.UNSUPPORTED_ORDER_TYPE and 400 <= exc.value.http_status < 500
    assert ctx.log == []


@pytest.mark.parametrize(
    "spec,ok",
    [
        (_stk(limit_price=Decimal("1.25")), True),
        (_stk(limit_price=Decimal("1.255")), False),  # stock >= $1: whole cents
        (_stk(limit_price=Decimal("0.5001")), True),  # stock < $1: $0.0001
        (_stk(limit_price=Decimal("0.50005")), False),
        (_opt(limit_price=Decimal("2.15")), True),
        (_opt(limit_price=Decimal("2.155")), False),  # no sub-penny option prices
    ],
)
def test_off_increment_price_is_refused_never_moved(spec, ok):
    ctx = FakeCtx()
    if ok:
        _place(ctx, spec)
        (kw,) = [k for n, _, k in ctx.log if n == "place_order"]
        assert kw["price"] == float(spec.limit_price) and kw["adjust_limit"] == 0  # sent unchanged, no server adjust
    else:
        with pytest.raises(FutuOrderRefused) as exc:
            _place(ctx, spec)
        assert exc.value.reason_code == fo.PRICE_PRECISION
        assert ctx.log == []


@pytest.mark.parametrize("price", [Decimal(0), Decimal("-1"), "nan", None, "abc"])
def test_invalid_price_is_refused(price):
    ctx = FakeCtx()
    with pytest.raises(FutuOrderRefused):
        _place(ctx, _stk(limit_price=price))
    assert ctx.log == []


def test_remark_is_xo_submission_id_and_capped_at_64_bytes():
    assert fo.remark_for("abc") == "xo:abc"
    ctx = FakeCtx()
    with pytest.raises(FutuOrderRefused) as exc:
        _place(ctx, _stk(), sid="x" * 62)  # "xo:" + 62 = 65 bytes
    assert exc.value.reason_code == fo.REMARK_TOO_LONG and ctx.log == []
    _place(ctx, _stk(), sid="x" * 61)
    assert [k["remark"] for n, _, k in ctx.log if n == "place_order"] == ["xo:" + "x" * 61]


def test_futu_codes_use_the_observation_format():
    from scripts.tests.test_futu_observation import CALL_CODE  # X3b fixture: US.SPY261218C500000

    assert fo.futu_code_for(_stk(symbol="BRK B")) == "US.BRK.B"
    assert fo.futu_code_for(_opt()) == CALL_CODE
    assert fo.futu_code_for(_opt(strike=Decimal("500.125"))) == "US.SPY261218C500125"
    for bad in (_opt(symbol="BRK B"), _opt(strike=Decimal("500.1255")), _opt(right="X"), _opt(expiry="2026-12-18")):
        with pytest.raises(FutuOrderRefused):
            fo.futu_code_for(bad)


# ---------------------------------------------------------------------------
# Place / modify / cancel: keyword calls, send mark, uncertain outcomes
# ---------------------------------------------------------------------------


def test_place_sends_keywords_after_the_mark_and_keeps_the_reported_code():
    ctx = FakeCtx()
    ctx.replies["place_order"] = (
        0,
        _frame([{"order_id": "8801", "code": "US.SPY261218C500000", "order_status": "SUBMITTED"}]),
    )

    out = _place(ctx, _opt())

    assert ctx.names() == ["mark", "place_order"]
    _, args, kw = ctx.log[1]
    assert args == ()
    assert kw == {
        "price": 2.15,
        "qty": 1,
        "code": "US.SPY261218C500000",
        "trd_side": "SELL",
        "order_type": "NORMAL",
        "adjust_limit": 0,
        "trd_env": "SIMULATE",
        "acc_id": int(ACC),
        "remark": "xo:sub-1",
        "time_in_force": "DAY",
    }
    assert (out.outcome, out.order_id, out.futu_code, out.state) == (
        "accepted",
        "8801",
        "US.SPY261218C500000",
        "WORKING",
    )


def test_place_reply_terminal_status_is_not_fresh_evidence():
    ctx = FakeCtx()
    ctx.replies["place_order"] = (0, _frame([{"order_id": "8802", "code": "US.SPY", "order_status": "FILLED_ALL"}]))
    assert _place(ctx, _stk()).state == "UNKNOWN"


@pytest.mark.parametrize(
    "reply",
    [
        TimeoutError("socket"),
        (-1, "Timeout"),
        (-1, "Connection closed"),
        (-1, "insufficient buying power"),
        (0, _frame([])),
    ],
)
def test_place_after_the_mark_is_never_not_sent(reply):
    ctx = FakeCtx()
    ctx.replies["place_order"] = reply
    out = _place(ctx, _stk())
    assert ctx.names() == ["mark", "place_order"]
    assert out.outcome == "unknown" and out.reason_code == fo.SEND_UNCERTAIN and out.order_id is None


def test_modify_and_cancel_ret_ok_is_sent_not_confirmed():
    ctx = FakeCtx()
    mod = fo.modify(
        ctx,
        _stk(quantity=7, limit_price=Decimal("1.30")),
        order_id="8801",
        account_env="paper",
        acc_id=ACC,
        mark_sent=ctx.mark,
        environ={},
    )
    can = fo.cancel(ctx, order_id="8801", account_env="paper", acc_id=ACC, mark_sent=ctx.mark, environ={})

    assert (mod.outcome, can.outcome) == ("sent", "sent") and mod.state == can.state == "UNKNOWN"
    assert ctx.names() == ["mark", "modify_order", "mark", "modify_order"]
    (_, a1, k1), (_, a2, k2) = ctx.log[1], ctx.log[3]
    assert a1 == a2 == ()
    assert (k1["modify_order_op"], k1["qty"], k1["price"], k1["adjust_limit"]) == ("NORMAL", 7, 1.30, 0)
    assert (k2["modify_order_op"], k2["order_id"], k2["trd_env"], k2["acc_id"]) == (
        "CANCEL",
        "8801",
        "SIMULATE",
        int(ACC),
    )


@pytest.mark.parametrize("reply", [ConnectionError("lost"), (-1, "Timeout")])
def test_modify_failure_after_the_mark_is_unknown(reply):
    ctx = FakeCtx()
    ctx.replies["modify_order"] = reply
    out = fo.cancel(ctx, order_id="8801", account_env="paper", acc_id=ACC, mark_sent=ctx.mark, environ={})
    assert out.outcome == "unknown" and out.reason_code == fo.SEND_UNCERTAIN


# ---------------------------------------------------------------------------
# Unlock (contract item 5)
# ---------------------------------------------------------------------------


def test_real_unlocks_by_keyword_before_the_mutation():
    ctx = FakeCtx()
    _place(ctx, _stk(), env="live", environ=REAL_ENV)
    assert ctx.names() == ["unlock_trade", "mark", "place_order"]
    assert ctx.log[0][1:] == ((), {"password_md5": SECRET})


def test_simulate_never_unlocks():
    ctx = FakeCtx()
    _place(ctx, _stk(), env="paper", environ=REAL_ENV)
    assert "unlock_trade" not in ctx.names()


@pytest.mark.parametrize(
    "environ,reply,code",
    [
        ({}, None, fo.UNLOCK_SECRET_MISSING),
        ({fo.PWD_MD5_ENV: "not-hex"}, None, fo.UNLOCK_SECRET_INVALID),
        ({fo.PWD_MD5_ENV: SECRET[:31]}, None, fo.UNLOCK_SECRET_INVALID),
        (REAL_ENV, (-1, f"bad password {SECRET}"), fo.UNLOCK_FAILED),
        (REAL_ENV, RuntimeError(f"boom {SECRET}"), fo.UNLOCK_FAILED),
    ],
)
def test_failed_unlock_refuses_every_real_mutation_and_never_leaks_the_secret(caplog, environ, reply, code):
    ctx = FakeCtx()
    if reply is not None:
        ctx.replies["unlock_trade"] = reply
    calls = [
        lambda: _place(ctx, _stk(), env="live", environ=environ),
        lambda: fo.modify(
            ctx, _stk(), order_id="8801", account_env="live", acc_id=ACC, mark_sent=ctx.mark, environ=environ
        ),
        lambda: fo.cancel(ctx, order_id="8801", account_env="live", acc_id=ACC, mark_sent=ctx.mark, environ=environ),
    ]
    texts = []
    with caplog.at_level(logging.DEBUG):
        for call in calls:
            with pytest.raises(FutuOrderRefused) as exc:
                call()
            assert exc.value.reason_code == code and exc.value.http_status in (403, 503)
            texts += [str(exc.value), repr(exc.value), str(exc.value.__cause__), str(exc.value.__context__)]

    assert not set(ctx.names()) & (MUTATIONS | {"mark"})  # zero SDK mutations, no send mark
    blob = "\n".join(texts + [caplog.text])
    for form in (SECRET, SECRET.lower(), SECRET.upper()):
        assert form not in blob


def test_secret_is_not_in_the_adapter_repr_or_results():
    ctx = FakeCtx()
    out = _place(ctx, _stk(), env="live", environ=REAL_ENV)
    assert SECRET not in repr(out) and SECRET.lower() not in repr(out).lower()


# ---------------------------------------------------------------------------
# Fresh evidence and recovery (contract items 4, 8)
# ---------------------------------------------------------------------------


def test_query_order_is_a_fresh_account_scoped_read():
    ctx = FakeCtx()
    ctx.replies["order_list_query"] = (0, _frame([{"order_id": "8801", "order_status": "CANCELLED_ALL", "qty": 10.0}]))

    ev = fo.query_order(ctx, order_id="8801", account_env="live", acc_id=ACC)

    assert ctx.log == [
        ("order_list_query", (), {"order_id": "8801", "trd_env": "REAL", "acc_id": int(ACC), "refresh_cache": True})
    ]
    assert (ev.state, ev.qty) == ("CANCELLED", Decimal("10.0"))
    ctx.replies["order_list_query"] = (0, _frame([]))
    assert fo.query_order(ctx, order_id="8801", account_env="live", acc_id=ACC) is None
    ctx.replies["order_list_query"] = (-1, "Timeout")
    with pytest.raises(fo.FutuReadFailed):
        fo.query_order(ctx, order_id="8801", account_env="live", acc_id=ACC)


def test_find_by_remark_matches_the_exact_remark_by_code_with_history():
    ctx = FakeCtx()
    code = "US.SPY"
    ctx.replies["order_list_query"] = (
        0,
        _frame(
            [
                {
                    "order_id": "1",
                    "code": code,
                    "remark": "xo:sub-10",
                    "order_status": "SUBMITTED",
                },  # longer id, not a match
                {"order_id": "2", "code": code, "remark": "", "order_status": "SUBMITTED"},
            ]
        ),
    )
    ctx.replies["history_order_list_query"] = (
        0,
        _frame([{"order_id": "3", "code": code, "remark": "xo:sub-1", "order_status": "FILLED_ALL"}]),
    )

    found = fo.find_by_remark(
        ctx,
        remark="xo:sub-1",
        futu_code=code,
        account_env="paper",
        acc_id=ACC,
        history_start="2026-10-01 00:00:00",
        history_end="2026-10-24 12:00:00",
    )

    assert found.status == "found" and found.order.order_id == "3" and found.order.state == "FILLED"
    assert ctx.log[0] == (
        "order_list_query",
        (),
        {"code": code, "trd_env": "SIMULATE", "acc_id": int(ACC), "refresh_cache": True},
    )
    assert ctx.log[1] == (
        "history_order_list_query",
        (),
        {
            "code": code,
            "start": "2026-10-01 00:00:00",
            "end": "2026-10-24 12:00:00",
            "trd_env": "SIMULATE",
            "acc_id": int(ACC),
        },
    )
    assert not set(ctx.names()) & MUTATIONS  # recovery never re-sends


def test_find_by_remark_absent_ambiguous_and_failed_read():
    ctx = FakeCtx()
    assert fo.find_by_remark(ctx, remark="xo:s", futu_code="US.SPY", account_env="paper", acc_id=ACC).status == "absent"
    assert [n for n in ctx.names()] == ["order_list_query"]  # no history without a window

    ctx.replies["order_list_query"] = (
        0,
        _frame(
            [
                {"order_id": "1", "remark": "xo:s"},
                {"order_id": "2", "remark": "xo:s"},
            ]
        ),
    )
    got = fo.find_by_remark(ctx, remark="xo:s", futu_code="US.SPY", account_env="paper", acc_id=ACC)
    assert got.status == "ambiguous" and got.order is None and len(got.orders) == 2

    ctx.replies["order_list_query"] = RuntimeError("down")
    with pytest.raises(fo.FutuReadFailed):
        fo.find_by_remark(ctx, remark="xo:s", futu_code="US.SPY", account_env="paper", acc_id=ACC)


def test_adapter_never_writes_fills():
    src = inspect.getsource(fo)
    assert "order_fills" not in src.replace("never writes `order_fills`", "")
    assert "mirror_futu_order" in src  # only named as the single fill writer
    assert "get_sync_engine" not in src and "insert(" not in src
