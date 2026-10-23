"""X3b Task 1: Futu observation pass + replay-safe order-grain fills.

The pass runs for real against Postgres (`committed_db`: each pass needs its
own transaction for `pass_seq`). The only fakes are the Futu trade context
(DataFrames with the futu-api 10.3.6308 column lists) and the IB
contract-metadata resolver. Gate 4 is evaluated with `gate4.load_inputs` on
the published snapshot. All quantities, prices, order ids and the account id
are made-up test values, not market data.

Port of `test_gate4_sync_pass.py:235-374`. Read order: acc list ->
positions -> open orders -> history orders; `on_read[name]` runs right after
read `name`, so an event can land between two reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import insert, select, text

from xenon.api.services import futu_observation as fo
from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots, order_fills, order_submissions
from xenon.execution import gate4, orders_store
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.committed_db

ACC = "281756479345015383"
OTHER_ACC = "281756479345015999"
SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account=ACC)
SCOPE_D = SCOPE.as_dict()
EXP = "20261218"
ET = ZoneInfo("America/New_York")

# futu-api 10.3.6308 column lists (open_trade_context.py).
ACC_COLS = [
    "acc_id",
    "trd_env",
    "acc_type",
    "uni_card_num",
    "card_num",
    "security_firm",
    "sim_acc_type",
    "trdmarket_auth",
    "acc_status",
    "acc_role",
    "jp_acc_type",
]
POS_COLS = [
    "code",
    "stock_name",
    "position_market",
    "qty",
    "can_sell_qty",
    "cost_price",
    "cost_price_valid",
    "average_cost",
    "diluted_cost",
    "market_val",
    "nominal_price",
    "pl_ratio",
    "pl_ratio_valid",
    "pl_ratio_avg_cost",
    "pl_val",
    "pl_val_valid",
    "today_buy_qty",
    "today_buy_val",
    "today_pl_val",
    "today_trd_val",
    "today_sell_qty",
    "today_sell_val",
    "position_side",
    "unrealized_pl",
    "realized_pl",
    "currency",
    "position_id",
]
ORDER_COLS = [
    "code",
    "stock_name",
    "order_market",
    "trd_side",
    "order_type",
    "order_status",
    "order_id",
    "qty",
    "price",
    "create_time",
    "updated_time",
    "dealt_qty",
    "dealt_avg_price",
    "last_err_msg",
    "remark",
    "time_in_force",
    "fill_outside_rth",
    "session",
    "aux_price",
    "trail_type",
    "trail_value",
    "trail_spread",
    "currency",
    "jp_acc_type",
]

CALL_CODE = f"US.SPY{EXP[2:]}C500000"
SPY_META = {
    "con_id": 111,
    "sym": "SPY",
    "sec": "OPT",
    "tc": "SPY",
    "mult": 100,
    "und_sec": "STK",
    "exp": EXP,
    "right": "C",
    "strike": 500.0,
}
QQQ_META = {
    "con_id": 222,
    "sym": "QQQ",
    "sec": "OPT",
    "tc": "QQQ",
    "mult": 100,
    "und_sec": "STK",
    "exp": EXP,
    "right": "C",
    "strike": 400.0,
}


def _frame(cols, rows):
    return pd.DataFrame([{c: r.get(c, "N/A") for c in cols} for r in rows], columns=cols)


def _acc_row(acc=ACC, env="SIMULATE", status="ACTIVE", auth=("US",)):
    return {
        "acc_id": int(acc),
        "trd_env": env,
        "acc_type": "MARGIN",
        "trdmarket_auth": list(auth),
        "acc_status": status,
    }


def _pos(code="US.SPY", qty=100.0, side=None):
    return {
        "code": code,
        "qty": qty,
        "position_side": side or ("LONG" if qty >= 0 else "SHORT"),
        "cost_price": 1.0,
        "nominal_price": 1.0,
        "market_val": qty,
        "currency": "USD",
        "position_market": "US",
    }


def _et(dt: datetime) -> str:
    return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")


def _order(
    order_id="7001",
    code=CALL_CODE,
    side="SELL",
    qty=1.0,
    status="SUBMITTED",
    dealt=0.0,
    avg="N/A",
    updated=None,
    remark="",
):
    updated = updated or _et(datetime.now(timezone.utc))
    return {
        "code": code,
        "trd_side": side,
        "order_type": "NORMAL",
        "order_status": status,
        "order_id": order_id,
        "qty": qty,
        "price": 1.25,
        "create_time": updated,
        "updated_time": updated,
        "dealt_qty": dealt,
        "dealt_avg_price": avg,
        "remark": remark,
        "currency": "USD",
        "order_market": "US",
    }


class FakeTradeCtx:
    """OpenSecTradeContext double (read methods only). Deal reads fail like
    SIMULATE and are asserted never called; there is no mutation method."""

    def __init__(self, *, positions=(), open_orders=(), history=(), accounts=None):
        self.accounts = list(accounts) if accounts is not None else [_acc_row(), _acc_row(OTHER_ACC)]
        self.positions = list(positions)
        self.open_orders = list(open_orders)
        self.history = list(history)
        self.calls: list[tuple[str, dict]] = []
        self.on_read: dict = {}
        self.fail: dict[str, tuple] = {}

    def _after(self, name, kwargs, result):
        self.calls.append((name, kwargs))
        if name in self.fail:
            return self.fail[name]
        hook = self.on_read.get(name)
        if hook:
            hook(self)
        return 0, result

    def get_acc_list(self):
        return self._after("get_acc_list", {}, _frame(ACC_COLS, self.accounts))

    def position_list_query(self, **kw):
        return self._after("positions", kw, _frame(POS_COLS, self.positions))

    def order_list_query(self, **kw):
        return self._after("open_orders", kw, _frame(ORDER_COLS, self.open_orders))

    def history_order_list_query(self, **kw):
        return self._after("history_orders", kw, _frame(ORDER_COLS, self.history))

    def deal_list_query(self, **kw):
        raise AssertionError("deal_list_query must never be called (SIMULATE: no deal data)")

    def history_deal_list_query(self, **kw):
        raise AssertionError("history_deal_list_query must never be called (SIMULATE: no deal data)")


def resolver(metas=(SPY_META, QQQ_META)):
    by_key = {(m["sym"], m["right"], m["exp"], m["strike"]): m for m in metas}

    def _resolve(specs):
        return [by_key.get((s["sym"], s["right"], s["exp"], s["strike"])) for s in specs]

    return _resolve


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)


def _run(ctx, metas=(SPY_META, QQQ_META)):
    return fo.run_pass(SCOPE, ctx, resolve_meta=resolver(metas))


def _latest_payload():
    with get_sync_engine().connect() as conn:
        row = conn.execute(
            select(account_snapshots.c.payload)
            .where(account_snapshots.c.broker == "FUTU")
            .order_by(account_snapshots.c.snapshot_at.desc())
            .limit(1)
        ).first()
    return row[0] if row else None


def _subs():
    with get_sync_engine().connect() as conn:
        return {r.broker_order_id: r for r in conn.execute(select(order_submissions)).all()}


def _fills():
    with get_sync_engine().connect() as conn:
        return conn.execute(select(order_fills).order_by(order_fills.c.exec_id)).all()


def _gate(sym="SPY", meta=SPY_META, qty=1):
    body = {
        "type": "option",
        "symbol": sym,
        "action": "SELL",
        "quantity": qty,
        "right": "C",
        "strike": meta["strike"],
        "expiry": EXP,
        "limitPrice": 1.0,
        "client_attempt_id": "cand",
    }
    with get_sync_engine().connect() as conn:
        inputs = gate4.load_inputs(conn, SCOPE_D)
    candidate, _ = gate4.candidate_from_place_body(body, ACC, leg_meta=[meta])
    return gate4._evaluate(inputs, candidate, netting=False)


def _read_names(ctx):
    return [name for name, _ in ctx.calls]


# ---------------------------------------------------------------------------
# control pass, read discipline
# ---------------------------------------------------------------------------


def test_control_pass_publishes_complete_and_accepts_covered_short():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)])
    result = _run(ctx)

    assert result["published"] and result["complete"], result
    assert _read_names(ctx) == ["get_acc_list", "positions", "open_orders", "history_orders"]
    kw = dict(ctx.calls)
    assert kw["positions"] == {"trd_env": "SIMULATE", "acc_id": int(ACC), "refresh_cache": True}
    assert kw["open_orders"] == {"trd_env": "SIMULATE", "acc_id": int(ACC), "refresh_cache": True}
    assert kw["history_orders"]["trd_env"] == "SIMULATE" and kw["history_orders"]["acc_id"] == int(ACC)
    obs = _latest_payload()["observation"]
    assert obs["complete"] is True and obs["account"] == ACC and obs["pass_seq"] and obs["pass_cluster"]
    leg = _latest_payload()["positions"][0]["legs"][0]
    assert leg == {**leg, "type": "Stock", "direction": "LONG", "contracts": 100, "account": ACC}
    assert _gate().accept is True


def test_history_window_covers_lag_across_date_boundary():
    as_of = datetime(2026, 11, 3, 5, 0, 30, tzinfo=timezone.utc)  # 00:00:30 ET
    start, end = fo._history_window(as_of)
    assert start == "2026-11-01 00:00:00"  # as_of - 60 s is 2026-11-02 23:59:30 ET; one more day back
    assert end == "2026-11-04 23:59:59"


def test_real_scope_reads_with_real_env(monkeypatch):
    ctx = FakeTradeCtx(positions=[], accounts=[_acc_row(env="REAL")])
    result = fo.run_pass(AccountScope(broker="FUTU", account_env="live", broker_account=ACC), ctx)
    assert result["complete"] is True
    assert all(kw.get("trd_env") == "REAL" for name, kw in ctx.calls if name != "get_acc_list")


# ---------------------------------------------------------------------------
# inter-read events (port of test_gate4_sync_pass.py:235-309)
# ---------------------------------------------------------------------------


def test_external_order_after_positions_read_blocks_next_short():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)])
    ctx.on_read["positions"] = lambda c: c.open_orders.append(_order("7001"))
    result = _run(ctx)

    assert result["complete"] is True
    row = _subs()["7001"]
    assert row.broker == "FUTU" and row.account_env == "paper" and row.broker_account == ACC
    assert row.submission_id == "futu-7001" and row.state == "WORKING" and row.action == "SELL"
    leg = row.legs["legs"][0]
    assert (leg["tc"], leg["mult"], leg["und_sec"], row.legs["account"]) == ("SPY", 100, "STK", ACC)
    decision = _gate()
    assert decision.accept is False and any(r.startswith("uncovered_call_increase") for r in decision.reasons)


@pytest.mark.parametrize("inject_after", ["positions", "open_orders"])
def test_external_short_placed_and_filled_between_reads_blocks_next_short(inject_after):
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)])
    filled = _order("7003", status="FILLED_ALL", dealt=1.0, avg=1.30)

    def _placed_and_filled(c):
        if inject_after == "positions":
            c.open_orders.append(filled)  # today's order list also shows filled orders
        c.history.append(filled)

    ctx.on_read[inject_after] = _placed_and_filled
    assert _run(ctx)["complete"] is True

    row = _subs()["7003"]
    assert row.state == "FILLED" and row.filled_qty == 1
    (fill,) = _fills()
    assert fill.exec_id == f"futu:{ACC}:ord:7003:1" and fill.qty == 1 and fill.submission_id == "futu-7003"
    assert fill.price == Decimal("1.3000") and fill.metadata["time_basis"] == "order_updated_time"
    assert _gate().accept is False


def test_working_order_filled_between_reads_blocks_next_short():
    working = _order("7002")
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[working])
    later = _et(datetime.now(timezone.utc) + timedelta(seconds=1))
    ctx.on_read["open_orders"] = lambda c: c.history.append(
        _order("7002", status="FILLED_ALL", dealt=1.0, avg=1.25, updated=later)
    )
    assert _run(ctx)["complete"] is True
    assert _subs()["7002"].state == "FILLED"
    assert [f.exec_id for f in _fills()] == [f"futu:{ACC}:ord:7002:1"]
    assert _gate().accept is False


def test_persist_failure_publishes_incomplete_snapshot(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(orders_store, "mirror_futu_order", _boom)
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7004")])
    result = _run(ctx)

    obs = _latest_payload()["observation"]
    assert result["published"] is True and obs["complete"] is False and obs["reads"]["persisted"] is False
    assert "view_incomplete" in _gate().reasons


# ---------------------------------------------------------------------------
# provenance (port of test_gate4_sync_pass.py:334-374)
# ---------------------------------------------------------------------------


def test_unkeyed_order_row_makes_pass_incomplete():
    """Futu rows carry no account; the pass attributes them by the verified
    acc_id it queried. A row without an order_id cannot be keyed (persisted)."""
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("")])
    _run(ctx)
    obs = _latest_payload()["observation"]
    assert obs["complete"] is False and obs["reads"]["orders_keyed"] is False
    assert "view_incomplete" in _gate().reasons


@pytest.mark.parametrize(
    "accounts",
    [
        [_acc_row(OTHER_ACC)],  # configured account absent
        [_acc_row(env="REAL")],  # configured account in the other env
        [_acc_row(status="DISABLED")],
        [_acc_row(auth=("HK",))],
    ],
)
def test_foreign_or_unverified_account_publishes_nothing_and_invalidates(accounts):
    assert _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)]))["complete"] is True
    assert _gate().accept is True

    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], accounts=accounts)
    result = _run(ctx)

    assert result["published"] is False and result["reads"] == {"account": False}
    assert _read_names(ctx) == ["get_acc_list"]  # no position/order read for an unverified account
    assert "view_incomplete" in _gate().reasons  # the older complete stamp no longer proves cover


def test_acc_list_read_failure_invalidates():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)])
    ctx.fail["get_acc_list"] = (-1, "OpenD offline")
    assert _run(ctx)["published"] is False
    assert _latest_payload() is None


def test_unsupported_read_publishes_incomplete():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)])
    ctx.fail["history_orders"] = (-1, "not supported")
    result = _run(ctx)
    assert result["published"] is True and result["complete"] is False
    assert _latest_payload()["observation"]["reads"]["history_orders"] is False
    assert "view_incomplete" in _gate().reasons


def test_positions_read_failure_persists_orders_and_invalidates():
    assert _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)]))["complete"] is True
    ctx = FakeTradeCtx(open_orders=[_order("7005")])
    ctx.fail["positions"] = (-1, "timeout")
    result = _run(ctx)
    assert result["published"] is False
    assert "7005" in _subs()  # demand still reached Postgres
    assert "view_incomplete" in _gate().reasons


def test_deal_reads_never_called_in_simulate():
    ctx = FakeTradeCtx(
        positions=[_pos("US.SPY", 100.0)],
        open_orders=[_order("7006", status="FILLED_PART", dealt=1.0, avg=1.0, qty=2.0)],
    )
    assert _run(ctx)["complete"] is True
    assert not any("deal" in name for name in _read_names(ctx))


def test_multi_leg_order_makes_pass_incomplete():
    ctx = FakeTradeCtx(
        positions=[_pos("US.SPY", 100.0)],
        open_orders=[_order("7007", code=CALL_CODE), _order("7007", code=f"US.SPY{EXP[2:]}C510000", side="BUY")],
    )
    _run(ctx)
    assert _latest_payload()["observation"]["reads"]["orders_keyed"] is False


def test_fractional_order_quantity_makes_pass_incomplete():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7008", code="US.SPY", qty=0.5)])
    _run(ctx)
    assert _latest_payload()["observation"]["reads"]["orders_parsed"] is False


def test_contradictory_position_side_makes_pass_incomplete():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0, side="SHORT")])
    _run(ctx)
    assert _latest_payload()["observation"]["reads"]["positions_parsed"] is False


def test_non_us_rows_are_left_out():
    ctx = FakeTradeCtx(
        positions=[_pos("US.SPY", 100.0), _pos("HK.00700", 100.0)],
        open_orders=[_order("7009", code="HK.00700", side="SELL", qty=100.0)],
    )
    assert _run(ctx)["complete"] is True
    assert [p["ticker"] for p in _latest_payload()["positions"]] == ["SPY"]
    assert "7009" not in _subs()


# ---------------------------------------------------------------------------
# unresolved holdings and legs fail their own symbol closed
# ---------------------------------------------------------------------------


def test_fractional_holding_fails_only_its_symbol():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.5), _pos("US.QQQ", 100.0)])
    assert _run(ctx)["complete"] is True
    assert _gate("SPY", SPY_META).accept is False
    assert _gate("QQQ", QQQ_META).accept is True


def test_unqualified_option_holding_and_order_fail_only_their_symbol():
    ctx = FakeTradeCtx(
        positions=[_pos("US.SPY", 200.0), _pos(CALL_CODE, -1.0), _pos("US.QQQ", 100.0)],
        open_orders=[_order("7010", code=f"US.SPY{EXP[2:]}C510000", side="SELL")],
    )
    assert fo.run_pass(SCOPE, ctx, resolve_meta=resolver([QQQ_META]))["complete"] is True
    held = _latest_payload()["positions"][1]["legs"][0]
    assert (held["tc"], held["mult"], held["und_sec"]) == (None, None, None)
    assert _subs()["7010"].legs["legs"] is None  # never default metadata
    assert _gate("SPY", SPY_META).accept is False
    assert _gate("QQQ", QQQ_META).accept is True


def test_resolver_mismatch_is_unresolved():
    wrong = {**SPY_META, "strike": 505.0}
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7011")])
    fo.run_pass(SCOPE, ctx, resolve_meta=lambda specs: [wrong for _ in specs])
    assert _subs()["7011"].legs["legs"] is None


# ---------------------------------------------------------------------------
# replay-safe high-water
# ---------------------------------------------------------------------------


def test_overlapping_open_and_history_rows_record_one_fill():
    row = _order("7020", status="FILLED_PART", qty=3.0, dealt=2.0, avg=1.10)
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 300.0)], open_orders=[row], history=[dict(row)])
    assert _run(ctx)["complete"] is True
    assert [(f.exec_id, f.qty) for f in _fills()] == [(f"futu:{ACC}:ord:7020:2", 2)]
    assert _subs()["7020"].filled_qty == 2


def test_restart_same_cumulative_records_nothing_new():
    row = _order("7021", status="FILLED_PART", qty=3.0, dealt=2.0, avg=1.10)
    for _ in range(3):
        assert _run(FakeTradeCtx(positions=[_pos("US.SPY", 300.0)], open_orders=[dict(row)]))["complete"] is True
    assert len(_fills()) == 1 and _subs()["7021"].filled_qty == 2


def test_stale_lower_cumulative_records_nothing():
    _run(
        FakeTradeCtx(
            positions=[_pos("US.SPY", 300.0)],
            open_orders=[_order("7022", qty=5.0, status="FILLED_PART", dealt=3.0, avg=1.0)],
        )
    )
    old = _order(
        "7022",
        qty=5.0,
        status="FILLED_PART",
        dealt=2.0,
        avg=1.0,
        updated=_et(datetime.now(timezone.utc) - timedelta(minutes=5)),
    )
    assert _run(FakeTradeCtx(positions=[_pos("US.SPY", 300.0)], history=[old]))["complete"] is True
    assert [f.qty for f in _fills()] == [3] and _subs()["7022"].filled_qty == 3


def test_cumulative_increase_records_delta_at_notional_price():
    _run(
        FakeTradeCtx(
            positions=[_pos("US.SPY", 500.0)],
            open_orders=[_order("7023", qty=5.0, status="FILLED_PART", dealt=2.0, avg=1.00)],
        )
    )
    _run(
        FakeTradeCtx(
            positions=[_pos("US.SPY", 500.0)],
            open_orders=[_order("7023", qty=5.0, status="FILLED_ALL", dealt=5.0, avg=1.30)],
        )
    )
    fills = {f.exec_id: f for f in _fills()}
    step = fills[f"futu:{ACC}:ord:7023:5"]
    assert step.qty == 3 and step.price == Decimal("1.5000")  # (5*1.30 - 2*1.00) / 3
    row = _subs()["7023"]
    assert (row.filled_qty, row.avg_fill_price, row.state) == (5, Decimal("1.3000"), "FILLED")


def test_fill_delta_without_avg_price_is_not_recorded_and_pass_incomplete():
    ctx = FakeTradeCtx(
        positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7024", status="FILLED_ALL", dealt=1.0, avg="N/A")]
    )
    _run(ctx)
    assert _fills() == [] and _subs()["7024"].filled_qty == 0
    assert _latest_payload()["observation"]["reads"]["persisted"] is False


def test_remark_binds_xenon_row_instead_of_new_external_row():
    now = datetime.now(timezone.utc)
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id="sub-xo-1",
                user_id="u",
                client_attempt_id="att-xo-1",
                ticker="SPY",
                security_type="OPT",
                action="SELL",
                quantity=1,
                state="PENDING",
                submitted_at=now,
                updated_at=now - timedelta(seconds=5),
                broker="FUTU",
                account_env="paper",
                broker_account=ACC,
            )
        )
    _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7025", remark="xo:sub-xo-1")]))
    subs = _subs()
    assert subs["7025"].submission_id == "sub-xo-1" and subs["7025"].state == "WORKING"
    assert not any(s.submission_id == "futu-7025" for s in subs.values())


def test_remark_never_binds_across_scope():
    now = datetime.now(timezone.utc)
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id="sub-xo-2",
                user_id="u",
                client_attempt_id="att-xo-2",
                ticker="SPY",
                security_type="OPT",
                action="SELL",
                quantity=1,
                state="PENDING",
                submitted_at=now,
                updated_at=now,
                broker="FUTU",
                account_env="live",
                broker_account=ACC,
            )
        )
    _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7026", remark="xo:sub-xo-2")]))
    assert _subs()["7026"].submission_id == "futu-7026"


def test_stale_observation_never_terminalizes():
    _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7027")]))
    with get_sync_engine().begin() as conn:  # a local write after the next read began
        conn.execute(
            text(
                "UPDATE xenon.order_submissions SET updated_at = now() + interval '1 hour' WHERE broker_order_id='7027'"
            )
        )
    _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7027", status="CANCELLED_ALL")]))
    assert _subs()["7027"].state == "WORKING"


# ---------------------------------------------------------------------------
# time: DST and the as_of boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wall", "utc"),
    [
        ("2026-07-01 10:00:00", "2026-07-01T14:00:00+00:00"),  # EDT
        ("2026-12-01 10:00:00", "2026-12-01T15:00:00+00:00"),  # EST
        ("2026-11-02 09:30:01", "2026-11-02T14:30:01+00:00"),  # first RTH after fall-back
        ("2026-11-01 01:30:00", "2026-11-01T06:30:00+00:00"),  # ambiguous: the later instant
        ("2026-03-09 09:30:00", "2026-03-09T13:30:00+00:00"),  # first RTH after spring-forward
        ("2026-10-23 12:00:00.500", "2026-10-23T16:00:00.500000+00:00"),
    ],
)
def test_futu_time_to_utc_is_dst_aware(wall, utc):
    assert fo.futu_time_to_utc(wall).isoformat() == utc


@pytest.mark.parametrize("bad", ["N/A", "", None, "garbage", 12])
def test_futu_time_to_utc_rejects_garbage(bad):
    assert fo.futu_time_to_utc(bad) is None


def test_fill_time_stored_as_utc_from_et_wall_time():
    wall = _et(datetime.now(timezone.utc) - timedelta(minutes=10))
    _run(
        FakeTradeCtx(
            positions=[_pos("US.SPY", 100.0)],
            history=[_order("7030", status="FILLED_ALL", dealt=1.0, avg=1.0, updated=wall)],
        )
    )
    (fill,) = _fills()
    assert fill.filled_at == datetime.strptime(wall, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET).astimezone(timezone.utc)


@pytest.mark.parametrize(("age", "accept"), [(timedelta(hours=2), True), (timedelta(seconds=30), False)])
def test_gate4_demand_is_conservative_across_as_of_boundary(age, accept):
    """200 shares, one short call already held (filled `age` before the pass).
    Two hours before: the position read shows it, the fill is before
    as_of - FILL_LAG_S, so 100 shares stay free. Thirty seconds before: the
    fill also counts as post-snapshot demand (double count, over-reserves)."""
    filled = _order("7031", status="FILLED_ALL", dealt=1.0, avg=1.0, updated=_et(datetime.now(timezone.utc) - age))
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 200.0), _pos(CALL_CODE, -1.0)], history=[filled])
    assert _run(ctx)["complete"] is True
    assert _gate().accept is accept


# ---------------------------------------------------------------------------
# publication fence and read-only
# ---------------------------------------------------------------------------


def test_stale_pass_is_not_published(monkeypatch):
    assert _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)]))["published"] is True
    seq = int(_latest_payload()["observation"]["pass_seq"])
    cluster = _latest_payload()["observation"]["pass_cluster"]
    monkeypatch.setattr(gate4, "next_pass_seq", lambda: (seq - 1, cluster))
    result = _run(FakeTradeCtx(positions=[_pos("US.SPY", 0.0)]))
    assert result["published"] is False and result["complete"] is False
    assert _latest_payload()["observation"]["pass_seq"] == str(seq)


def test_missing_pass_seq_publishes_nothing_and_invalidates(monkeypatch):
    assert _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)]))["complete"] is True

    def _no_seq():
        raise RuntimeError("no xid")

    monkeypatch.setattr(gate4, "next_pass_seq", _no_seq)
    assert _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)]))["published"] is False
    assert "view_incomplete" in _gate().reasons


def test_read_only_skips_everything(monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7040")])
    assert fo.run_pass(SCOPE, ctx)["published"] is False
    assert ctx.calls == []
    assert _latest_payload() is None and _subs() == {}


def test_non_futu_scope_refused():
    with pytest.raises(ValueError):
        fo.run_pass(AccountScope(broker="IB", account_env="paper", broker_account="DU1"), FakeTradeCtx())
