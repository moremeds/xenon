"""PR #236 review round 1 (Astra F1-F8) regressions for the Futu
observation pass, loop, display reads and refresh.

Real Postgres (`committed_db`), fake Futu trade context and fake IB
metadata resolver. All ids, quantities and prices are made-up test values,
not market data.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select

from scripts.tests.test_futu_observation import (  # noqa: F401 — autouse fixture
    ACC,
    SCOPE,
    SCOPE_D,
    FakeTradeCtx,
    _env,
    _fills,
    _gate,
    _order,
    _pos,
    _subs,
    resolver,
)
from scripts.tests.test_futu_observation_loop import Connector
from xenon.api.services import futu_observation as fo
from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots, order_submissions
from xenon.execution import gate4
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.committed_db


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", ACC)
    monkeypatch.setenv("FUTU_TRADE_ENV", "SIMULATE")


def _run(ctx, metas=None):
    return fo.run_pass(SCOPE, ctx, resolve_meta=resolver() if metas is None else resolver(metas))


def _view_complete() -> bool:
    with get_sync_engine().connect() as conn:
        return gate4.load_inputs(conn, SCOPE_D).view.complete


def _observation_rows() -> int:
    with get_sync_engine().connect() as conn:
        return conn.execute(
            select(func.count())
            .select_from(account_snapshots)
            .where(account_snapshots.c.broker == "FUTU", account_snapshots.c.payload.has_key("observation"))
        ).scalar()


# ---------------------------------------------------------------------------
# F1 — a remark (or stored broker id) is not proof of contract identity
# ---------------------------------------------------------------------------


def _reserve_buy_stock(sid="sub-buy-stk"):
    now = datetime.now(timezone.utc)
    legs = {
        "legs": [{"action": "BUY", "sym": "SPY", "sec": "STK", "ratio": 1}],
        "guaranteed": None,
        "pending_amend_qty": None,
        "account": ACC,
    }
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=sid,
                user_id="u",
                client_attempt_id=f"att-{sid}",
                ticker="SPY",
                security_type="STK",
                action="BUY",
                quantity=100,
                state="PENDING",
                submitted_at=now,
                updated_at=now - timedelta(seconds=5),
                broker="FUTU",
                account_env="paper",
                broker_account=ACC,
                legs=legs,
            )
        )


def test_f1_remark_on_a_contradicting_order_does_not_bind_and_fails_closed():
    _reserve_buy_stock()
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7101", remark="xo:sub-buy-stk")])

    result = _run(ctx)

    assert result["complete"] is False
    subs = _subs()
    assert "7101" not in subs  # neither bound nor inserted as a new row
    with get_sync_engine().connect() as conn:
        row = conn.execute(select(order_submissions).where(order_submissions.c.submission_id == "sub-buy-stk")).one()
    assert (row.broker_order_id, row.action, row.security_type, row.state) == (None, "BUY", "STK", "PENDING")
    assert _gate().accept is False  # the hidden SELL call cannot free cover


def test_f1_stored_broker_id_contradicted_later_is_not_overwritten():
    assert _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order("7102", qty=1.0)]))["complete"]
    contradicting = _order("7102", code="US.SPY", side="BUY", qty=1.0)

    result = _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[contradicting]))

    assert result["complete"] is False
    row = _subs()["7102"]
    assert (row.security_type, row.action, row.right) == ("OPT", "SELL", "C")


# ---------------------------------------------------------------------------
# F2 — only rows positively outside the US are left out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,row",
    [
        ("open_orders", {**_order("review-missing", side="SELL"), "code": "N/A", "order_market": "US"}),
        ("open_orders", {**_order("review-hk-us", code="HK.00700", side="SELL"), "order_market": "US"}),
        ("history", {**_order("review-blank", side="SELL"), "code": "", "order_market": "US"}),
        ("positions", {**_pos("US.SPY", 10.0), "code": "N/A"}),
        ("positions", {**_pos("SPY", 10.0)}),  # no market prefix
    ],
)
def test_f2_unclassifiable_rows_make_the_pass_incomplete(field, row):
    kw = {"positions": [_pos("US.SPY", 100.0)], "open_orders": [], "history": []}
    kw[field] = [*kw[field], row]

    result = _run(FakeTradeCtx(**kw))

    assert result["complete"] is False
    assert result["reads"]["rows_classified"] is False
    assert _gate().accept is False


# ---------------------------------------------------------------------------
# F3 — a timed-out pass invalidates the view and can never publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f3_timeout_invalidates_at_once_and_late_completion_does_not_publish():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)])
    loop = fo.ObservationLoop(SCOPE, connect=Connector(ctx), resolve_meta=resolver(), pass_timeout_s=0.3)
    assert (await loop.tick())["complete"] is True
    assert _view_complete() is True
    published_before = _observation_rows()
    release = threading.Event()
    ctx.on_read["positions"] = lambda _ctx: release.wait(5)

    assert await loop.tick() is None  # timed out

    assert _view_complete() is False  # failed closed immediately
    assert _gate().accept is False
    release.set()
    for _ in range(100):
        if loop._inflight.done():
            break
        await asyncio.sleep(0.02)
    late = loop._inflight.result()
    assert late["published"] is False
    assert _observation_rows() == published_before
    assert _view_complete() is False
    await loop.stop()


@pytest.mark.asyncio
async def test_f3_connection_finishing_after_timeout_runs_no_pass():
    release = threading.Event()
    connector = Connector()
    runs = []

    def slow_connect():
        release.wait(5)
        return connector()

    loop = fo.ObservationLoop(SCOPE, connect=slow_connect, run=lambda *a, **k: runs.append(1) or {}, pass_timeout_s=0.1)

    assert await loop.tick() is None
    assert _view_complete() is False
    release.set()
    for _ in range(100):
        if loop._inflight.done():
            break
        await asyncio.sleep(0.02)
    assert loop._inflight.result()["error"] == "retired"
    assert runs == [] and connector.closes == 1
    await loop.stop()


# ---------------------------------------------------------------------------
# F4 — display reads are scoped (broker, env, account)
# ---------------------------------------------------------------------------


A_LIVE = AccountScope(broker="FUTU", account_env="live", broker_account="111000111")
B_LIVE = AccountScope(broker="FUTU", account_env="live", broker_account="222000222")
A_PAPER = AccountScope(broker="FUTU", account_env="paper", broker_account="111000111")


def _store_display(scope: AccountScope, count: int, warnings=()):
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account=scope.broker_account,
                bankroll=Decimal(1),
                payload={"count": count, "positions": [], "warnings": list(warnings), "fetched_at": f"t{count}"},
                broker="FUTU",
                account_env=scope.account_env,
                broker_account=scope.broker_account,
                snapshot_at=func.clock_timestamp(),
            )
        )


def _portfolio(monkeypatch, request_scope):
    from fastapi.testclient import TestClient

    from xenon.api import guards, server
    from xenon.api.routes import futu as futu_routes

    offline = type("Offline", (), {"is_connected": lambda self: False})()
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda: offline)
    # The configured trade scope is the FUTU read scope (R2 F5).
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", request_scope.broker_account)
    monkeypatch.setenv("FUTU_TRADE_ENV", "REAL" if request_scope.account_env == "live" else "SIMULATE")
    monkeypatch.setattr(guards, "_futu_scope_from_db", lambda: None)
    return TestClient(server.app).get("/futu/portfolio").json()


def test_f4_portfolio_serves_only_the_request_account(monkeypatch):
    _store_display(A_LIVE, 1)
    _store_display(B_LIVE, 2)  # newest, another account

    body = _portfolio(monkeypatch, A_LIVE)

    assert body["ok"] is True and body["count"] == 1
    assert body["scope"] == {"broker": "FUTU", "accountEnv": "live", "brokerAccount": "111000111"}


def test_f4_paper_and_live_never_blend(monkeypatch):
    _store_display(A_LIVE, 1)
    _store_display(A_PAPER, 3)

    assert _portfolio(monkeypatch, A_LIVE)["count"] == 1
    assert _portfolio(monkeypatch, A_PAPER)["count"] == 3


def test_f4_other_scope_only_is_a_mismatch_not_its_data(monkeypatch):
    _store_display(B_LIVE, 2)

    body = _portfolio(monkeypatch, A_LIVE)

    assert body["ok"] is False and body["code"] == "scope_mismatch" and body["count"] == 0


def test_f4_partial_failure_compares_only_the_same_scope():
    from xenon.api.routes import futu as futu_routes

    _store_display(B_LIVE, 27)
    degraded = {"count": 5, "positions": [], "warnings": ["row skipped"]}

    assert futu_routes._maybe_preserve_partial_failure(degraded, A_LIVE) is None
    _store_display(A_LIVE, 27)
    assert futu_routes._maybe_preserve_partial_failure(degraded, A_LIVE)["count"] == 27
    assert futu_routes._maybe_preserve_partial_failure(degraded, A_PAPER) is None


def test_f4_health_uses_one_scope(monkeypatch):
    from fastapi.testclient import TestClient

    from xenon.api import server

    monkeypatch.setenv("FUTU_TRADE_ACC_ID", "111000111")
    monkeypatch.setenv("FUTU_TRADE_ENV", "REAL")
    _store_display(A_LIVE, 1)
    _store_display(B_LIVE, 2)

    futu = TestClient(server.app).get("/health").json()["futu"]

    assert futu["last_sync_at"] == "t1" and futu["scope_env"] == "live"


# ---------------------------------------------------------------------------
# F5 — precise cumulative notional, not the 4-place display average
# ---------------------------------------------------------------------------


def test_f5_high_precision_partial_fill_across_restart_conserves_notional():
    first_avg = 1.000049
    second_avg = (10_000 * first_avg + 1.0) / 10_001
    _run(
        FakeTradeCtx(
            open_orders=[
                _order(
                    "7501", code="US.SPY", side="BUY", qty=20_000.0, status="FILLED_PART", dealt=10_000.0, avg=first_avg
                )
            ]
        )
    )
    second = _order(
        "7501", code="US.SPY", side="BUY", qty=20_000.0, status="FILLED_PART", dealt=10_001.0, avg=second_avg
    )

    assert _run(FakeTradeCtx(open_orders=[second]))["complete"] is True
    assert _run(FakeTradeCtx(open_orders=[second]))["complete"] is True  # restart: same cumulative again

    fills = _fills()
    assert [f.qty for f in fills] == [Decimal(10_000), Decimal(1)]
    assert fills[1].price == Decimal("1.0000")
    notional = sum(f.qty * f.price for f in fills)
    expected = Decimal(10_001) * Decimal(str(second_avg))
    assert abs(notional - expected) <= Decimal("0.0001") * Decimal(10_001)
    assert _subs()["7501"].legs["futu_fill"]["cum_qty"] == 10_001


def test_f5_non_positive_notional_change_is_unresolved_not_fabricated():
    _run(
        FakeTradeCtx(
            open_orders=[_order("7502", code="US.SPY", side="BUY", qty=10.0, status="FILLED_PART", dealt=5.0, avg=2.0)]
        )
    )
    lower = _order("7502", code="US.SPY", side="BUY", qty=10.0, status="FILLED_PART", dealt=6.0, avg=1.5)

    result = _run(FakeTradeCtx(open_orders=[lower]))

    assert result["complete"] is False
    assert [f.qty for f in _fills()] == [Decimal(5)]
    assert _subs()["7502"].filled_qty == 5


# ---------------------------------------------------------------------------
# F6 — order type, trigger price and TIF survive the ledger
# ---------------------------------------------------------------------------


def _open_payload():
    from xenon.api.routes.orders import orders_payload_for_scope

    return {r["brokerOrderId"]: r for r in orders_payload_for_scope(SCOPE)["open_orders"]}


def test_f6_gtc_stop_limit_round_trips_and_updates():
    row = {
        **_order("7601", code="US.SPY", side="SELL", qty=10.0),
        "order_type": "STOP_LIMIT",
        "time_in_force": "GTC",
        "aux_price": 1.20,
    }
    plain = {**_order("7602", code="US.SPY", side="BUY", qty=10.0), "order_type": "NORMAL", "time_in_force": "DAY"}
    _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[row, plain]))

    got = _open_payload()
    assert (got["7601"]["orderType"], got["7601"]["tif"], got["7601"]["auxPrice"]) == ("STOP_LIMIT", "GTC", 1.2)
    assert (got["7602"]["orderType"], got["7602"]["tif"], got["7602"]["auxPrice"]) == ("LMT", "DAY", None)

    time.sleep(0.01)
    _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[{**row, "aux_price": 1.25}, plain]))
    assert _open_payload()["7601"]["auxPrice"] == 1.25


# ---------------------------------------------------------------------------
# F7 — FUTU refresh runs the observation pass and returns the ledger
# ---------------------------------------------------------------------------


def _refresh(monkeypatch, request_scope, loop):
    from fastapi.testclient import TestClient

    from xenon.api import guards, server
    from xenon.api.routes import futu as futu_routes
    from xenon.api.routes import portfolio as portfolio_routes

    async def _no_history(*a, **k):
        raise AssertionError("refresh must not run the history sync")

    offline = type("Offline", (), {"is_connected": lambda self: False})()
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda: offline)
    monkeypatch.setattr(guards, "_futu_scope_from_db", lambda: request_scope)
    monkeypatch.setattr(portfolio_routes, "_futu_refresh_db_first", _no_history)
    monkeypatch.setattr(server, "_is_test_mode", lambda: False)
    monkeypatch.setattr(server.app.state, "futu_observation_loop", loop, raising=False)
    # No `with`: the lifespan must not run (test mode is off for this request).
    return TestClient(server.app).post("/orders/refresh", params={"broker": "FUTU"})


def test_f7_refresh_observes_a_cancel_and_a_fill(monkeypatch):
    working = _order("7701", code="US.SPY", side="BUY", qty=10.0)
    to_cancel = _order("7702", code="US.SPY", side="BUY", qty=5.0)
    assert _run(FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[working, to_cancel]))["complete"]
    time.sleep(0.01)
    ctx = FakeTradeCtx(
        positions=[_pos("US.SPY", 110.0)],
        history=[
            {**working, "order_status": "FILLED_ALL", "dealt_qty": 10.0, "dealt_avg_price": 2.0},
            {**to_cancel, "order_status": "CANCELLED_ALL"},
        ],
    )
    loop = fo.ObservationLoop(SCOPE, connect=Connector(ctx), resolve_meta=resolver())

    resp = _refresh(monkeypatch, SCOPE, loop)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["open_orders"] == []
    assert [r["brokerOrderId"] for r in body["executed_orders"]] == ["7701"]
    assert (_subs()["7701"].state, _subs()["7702"].state) == ("FILLED", "CANCELLED")


def test_f7_mismatched_refresh_runs_no_pass(monkeypatch):
    """A loop of another scope than the request (configured) scope never runs."""
    connector = Connector()
    other = AccountScope(broker="FUTU", account_env="live", broker_account=ACC)
    loop = fo.ObservationLoop(other, connect=connector, resolve_meta=resolver())

    body = _refresh(monkeypatch, other, loop).json()

    assert body["scope"]["accountEnv"] == "paper" and connector.connects == 0


# ---------------------------------------------------------------------------
# F8 — the worker owns context retirement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f8_connection_arriving_after_stop_is_closed_and_runs_nothing():
    release = threading.Event()
    connector = Connector()
    runs = []

    def slow_connect():
        release.wait(5)
        return connector()

    loop = fo.ObservationLoop(SCOPE, connect=slow_connect, run=lambda *a, **k: runs.append(1) or {})
    loop.start()
    await asyncio.sleep(0.05)  # the first pass is now inside connect

    await asyncio.wait_for(loop.stop(wait_s=0.1), 2)
    release.set()
    for _ in range(100):
        if connector.closes:
            break
        await asyncio.sleep(0.02)

    assert runs == [] and connector.closes == 1
    assert loop._ctx is None


@pytest.mark.asyncio
async def test_f8_stop_during_pass_retires_it_and_closes_the_context():
    release = threading.Event()
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)])
    ctx.on_read["positions"] = lambda _ctx: release.wait(5)
    connector = Connector(ctx)
    loop = fo.ObservationLoop(SCOPE, connect=connector, resolve_meta=resolver())
    before = _observation_rows()
    loop.start()
    await asyncio.sleep(0.1)

    await asyncio.wait_for(loop.stop(wait_s=0.1), 2)
    release.set()
    for _ in range(100):
        if connector.closes:
            break
        await asyncio.sleep(0.02)

    assert connector.closes == 1 and _observation_rows() == before

