"""X3b Task 2: the Futu observation loop, the IB metadata resolver and the
shadow naked-short audit on complete FUTU passes.

The Futu trade context is the fake from `test_futu_observation.py`; passes
run for real against Postgres (`committed_db`). All ids, quantities and
prices are made-up test values, not market data.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from sqlalchemy import select

from scripts.tests.test_futu_observation import (  # noqa: F401 — autouse fixture
    ACC,
    SCOPE,
    SCOPE_D,
    SPY_META,
    FakeTradeCtx,
    _env,
    _order,
    _pos,
    resolver,
)
from xenon.api.services import futu_observation as fo
from xenon.db.engine import get_sync_engine
from xenon.db.schema import service_health
from xenon.execution import gate4

pytestmark = pytest.mark.committed_db


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)
    monkeypatch.delenv("XENON_FUTU_TRADING", raising=False)


class Connector:
    """Connector double: returns a context (or raises) and counts closes."""

    def __init__(self, ctx=None, error=None):
        self.ctx = ctx or FakeTradeCtx(positions=[_pos("US.SPY", 100.0)])
        self.error = error
        self.connects = 0
        self.closes = 0
        self.threads: list[str] = []

    def __call__(self):
        self.connects += 1
        self.threads.append(threading.current_thread().name)
        if self.error is not None:
            raise self.error
        return self.ctx, self._close

    def _close(self):
        self.closes += 1


def _loop(connector, **kw):
    kw.setdefault("resolve_meta", resolver())
    return fo.ObservationLoop(SCOPE, connect=connector, **kw)


def _audit_row():
    with get_sync_engine().connect() as conn:
        return conn.execute(select(service_health).where(service_health.c.service == "naked_short_audit")).first()


# ---------------------------------------------------------------------------
# start conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_skips_the_loop(monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    made = []

    loops = fo.start_observation_loops(connector=lambda scope: made.append(scope))

    assert loops is None and made == []


async def _loop_of(loops, scope=SCOPE):
    for _ in range(100):
        if loops.loop_for(scope) is not None:
            return loops.loop_for(scope)
        await asyncio.sleep(0.02)
    raise AssertionError(f"no loop for {scope}")


@pytest.mark.asyncio
async def test_no_configured_scope_runs_no_loop(monkeypatch):
    monkeypatch.delenv("FUTU_TRADE_ACC_ID_SIMULATE")
    made = []

    loops = fo.start_observation_loops(connector=lambda scope: made.append(scope) or Connector())
    await asyncio.sleep(0.1)

    assert loops is not None and loops.running and loops.loops == {} and made == []
    await loops.stop()


@pytest.mark.asyncio
async def test_loop_starts_without_trading_capability_and_stops_cleanly(monkeypatch):
    monkeypatch.setenv("XENON_FUTU_TRADING", "0")
    connector = Connector()
    seen = []

    loops = fo.start_observation_loops(resolve_meta=resolver(), connector=lambda scope: seen.append(scope) or connector)
    loop = await _loop_of(loops)
    assert loop.running and seen == [SCOPE]
    for _ in range(100):
        if loop.last_result is not None:
            break
        await asyncio.sleep(0.02)
    assert loop.last_result["complete"] is True

    await asyncio.wait_for(loops.stop(), 2)

    assert not loop.running
    await asyncio.sleep(0.05)  # the close runs on a daemon thread
    assert connector.closes == 1


# ---------------------------------------------------------------------------
# tick behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_runs_on_the_loop_thread_never_the_event_loop():
    connector = Connector()
    loop = _loop(connector)

    result = await loop.tick()

    assert result["complete"] is True
    assert connector.threads and connector.threads[0].startswith("xenon-futu-observe")
    assert connector.threads[0] != threading.current_thread().name
    await loop.stop()


@pytest.mark.asyncio
async def test_opend_offline_invalidates_view_and_reconnects_later():
    connector = Connector(error=ConnectionRefusedError("OpenD down"))
    loop = _loop(connector)

    result = await loop.tick()

    assert result == {"published": False, "complete": False, "error": "connect_failed"}
    with get_sync_engine().connect() as conn:
        assert gate4.view_watermark(conn, SCOPE_D) is not None
    connector.error = None
    assert (await loop.tick())["complete"] is True
    assert connector.connects == 2
    await loop.stop()


@pytest.mark.asyncio
async def test_failed_account_read_reopens_the_context():
    ctx = FakeTradeCtx(accounts=[])
    connector = Connector(ctx)
    loop = _loop(connector)

    assert (await loop.tick())["complete"] is False
    assert connector.closes == 1
    ctx.accounts = FakeTradeCtx().accounts
    assert (await loop.tick())["complete"] is True
    assert connector.connects == 2
    await loop.stop()


@pytest.mark.asyncio
async def test_pass_exception_resets_context_and_invalidates():
    def boom(*a, **k):
        raise RuntimeError("pass bug")

    connector = Connector()
    loop = _loop(connector, run=boom)

    result = await loop.tick()

    assert result["error"] == "RuntimeError" and connector.closes == 1
    with get_sync_engine().connect() as conn:
        assert gate4.view_watermark(conn, SCOPE_D) is not None
    await loop.stop()


@pytest.mark.asyncio
async def test_slow_pass_times_out_and_blocks_overlap():
    release = threading.Event()

    def slow(*a, **k):
        release.wait(5)
        return {"complete": True, "reads": {"account": True}}

    loop = _loop(Connector(), run=slow, pass_timeout_s=0.1)

    assert await loop.tick() is None  # timed out
    assert await loop.tick() is None  # previous pass still running: skipped
    release.set()
    await asyncio.sleep(0.1)
    assert (await loop.tick())["complete"] is True
    await loop.stop()


# ---------------------------------------------------------------------------
# shadow naked-short audit (complete FUTU passes only, never cancels)
# ---------------------------------------------------------------------------


def test_complete_pass_runs_shadow_audit_under_futu_scope():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order(order_id="7001", qty=2.0)])

    result = fo.run_pass(SCOPE, ctx, resolve_meta=resolver())

    assert result["complete"] is True
    assert result["audit"]["status"] == "violations"  # 2 short calls vs 100 shares
    assert [v["order_id"] for v in result["audit"]["violations"]] == ["7001"]
    row = _audit_row()
    assert (row.broker, row.account_env, row.broker_account) == ("FUTU", "paper", ACC)
    assert row.state == "violations" and json.loads(row.detail)["mode"] == "shadow"
    assert not hasattr(ctx, "cancel_all_order") and not hasattr(ctx, "modify_order")


def test_covered_short_audits_ok():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order(order_id="7001", qty=1.0)])

    result = fo.run_pass(SCOPE, ctx, resolve_meta=resolver())

    assert result["audit"]["status"] == "ok"
    assert _audit_row().state == "ok"


def test_unresolved_option_order_symbol_is_skipped_not_flagged():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order(order_id="7001", qty=5.0)])

    result = fo.run_pass(SCOPE, ctx, resolve_meta=resolver(metas=()))

    assert result["audit"]["status"] == "ok" and result["audit"]["violations"] == []
    assert {s["symbol"] for s in result["audit"]["skipped"]} == {"SPY"}


def test_incomplete_pass_runs_no_audit():
    ctx = FakeTradeCtx(positions=[_pos("US.SPY", 100.0)], open_orders=[_order(order_id="7001", qty=2.0)])
    ctx.fail["history_orders"] = (-1, "unsupported")

    result = fo.run_pass(SCOPE, ctx, resolve_meta=resolver())

    assert result["complete"] is False and result["audit"] is None
    assert _audit_row() is None


# ---------------------------------------------------------------------------
# IB contract-metadata resolver (place-gate path, fail closed)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_meta_cache(monkeypatch):
    monkeypatch.setattr(fo, "_IB_META_CACHE", {}, raising=False)


class _Pool:
    def __init__(self, fail=False, connected=True):
        self.fail = fail
        self.connected = connected
        self.roles = []
        self.calls = 0

    def is_connected(self, role):
        assert role == "data"
        return self.connected

    def acquire(self, role):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                pool.roles.append(role)
                return "ib-client"

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def run_sync(self, role, fn, client, specs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("data role down")
        assert (role, fn.__name__, client) == ("data", "_resolve_leg_meta_with_client", "ib-client")
        return [SPY_META for _ in specs]


@pytest.mark.asyncio
async def test_resolver_qualifies_through_the_pool_data_role():
    pool = _Pool()
    resolve = fo.make_ib_meta_resolver(asyncio.get_running_loop(), lambda: pool)
    spec = {"sym": "SPY", "right": "C", "exp": "20261218", "strike": 500.0, "con_id": None}

    got = await asyncio.to_thread(resolve, [spec])

    assert got == [SPY_META] and pool.roles == ["data"]


@pytest.mark.asyncio
async def test_resolver_fails_closed_without_pool_or_on_error():
    spec = {"sym": "SPY", "right": "C", "exp": "20261218", "strike": 500.0, "con_id": None}
    loop = asyncio.get_running_loop()

    assert await asyncio.to_thread(fo.make_ib_meta_resolver(loop, lambda: None), [spec]) == [None]
    assert await asyncio.to_thread(fo.make_ib_meta_resolver(loop, lambda: _Pool(fail=True)), [spec]) == [None]



# ---------------------------------------------------------------------------
# lifespan wiring (start on boot, stop on shutdown, skipped under read-only)
# ---------------------------------------------------------------------------


@pytest.fixture
def boot(monkeypatch):
    """Real (non-test-mode) lifespan with IB, DB init and other loops stubbed,
    and the observation loop's connector replaced (no OpenD access)."""
    from xenon.api import server as server_mod
    from xenon.api import trading_mode

    class _Pool:
        async def connect_all(self):
            return {}

        async def disconnect_all(self, **kwargs):
            return None

    class _Journal:
        async def start(self):
            return None

        async def stop(self):
            return None

    async def _noop(*a, **k):
        return None

    async def _gw():
        return {"port_listening": True}

    monkeypatch.setenv("XENON_API_TEST_MODE", "0")
    monkeypatch.setattr(server_mod, "validate_auth_config", lambda: "auth posture: test")
    monkeypatch.setattr(server_mod, "ensure_ib_gateway", _gw)
    monkeypatch.setattr(server_mod, "IBPool", _Pool)
    monkeypatch.setattr(server_mod.orders_store, "init_store", lambda *a, **k: None)
    monkeypatch.setattr(server_mod, "init_engine", lambda *a, **k: None)
    monkeypatch.setattr(server_mod, "dispose_engine", _noop)
    monkeypatch.setattr(server_mod, "_maybe_start_futu_history_loop", lambda: None)
    monkeypatch.setattr(server_mod, "_maybe_start_wizard_risk_alert", lambda: None)
    monkeypatch.setattr(server_mod, "_run_rehydrate_on_boot", _noop)
    monkeypatch.setattr(server_mod, "_run_fills_replay_on_boot", _noop)
    monkeypatch.setattr(server_mod, "_maybe_start_activity_poller", lambda: None)
    monkeypatch.setattr(
        "xenon.api.services.journal_auto_import.JournalAutoImportSubscriber", lambda *a, **k: _Journal()
    )
    monkeypatch.setattr(server_mod, "ib_pool", None)
    account = "DU0000000" if trading_mode.MODE == "paper" else "U0000000"
    monkeypatch.setattr(server_mod, "_get_managed_account_for_health", lambda: account)

    connector = Connector()
    real_start = fo.start_observation_loops
    monkeypatch.setattr(
        fo, "start_observation_loops", lambda **kw: real_start(connector=lambda scope: connector, **kw)
    )

    async def _run():
        seen = {}
        async with server_mod.lifespan(server_mod.app):
            loops = server_mod.app.state.futu_observation_loops
            seen["loop"] = await _loop_of(loops) if loops is not None else None
            if seen["loop"] is not None:
                for _ in range(100):
                    if seen["loop"].last_result is not None:
                        break
                    await asyncio.sleep(0.02)
                seen["running"] = seen["loop"].running
        return seen, connector

    return _run


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_the_observation_loop(boot, monkeypatch):
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)

    seen, connector = await boot()

    loop = seen["loop"]
    assert isinstance(loop, fo.ObservationLoop) and seen["running"] is True
    assert loop.last_result["complete"] is True
    assert not loop.running  # stopped by the lifespan shutdown
    await asyncio.sleep(0.05)
    assert connector.closes == 1


@pytest.mark.asyncio
async def test_lifespan_skips_the_observation_loop_under_read_only(boot, monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")

    seen, connector = await boot()

    assert seen["loop"] is None and connector.connects == 0


def test_newer_display_snapshot_does_not_replace_the_gate4_view():
    """A /futu/sync display row for the same scope (no observation stamp) is
    newer than the observation pass; Gate 4 still reads the pass."""
    from unittest.mock import MagicMock

    from scripts.tests.test_futu_observation import _gate
    from xenon.api.routes import futu as futu_routes

    assert fo.run_pass(SCOPE, FakeTradeCtx(positions=[_pos("US.SPY", 100.0)]), resolve_meta=resolver())["complete"]
    client = MagicMock()
    client._acc_id = int(ACC)
    client.trd_env_of_matched_account.return_value = "SIMULATE"
    display = {"fetched_at": "2026-10-24T12:00:00Z", "positions": [], "count": 0, "account_summary": {"net_liquidation": 1.0}}
    assert futu_routes._persist_display_snapshot(client, display) is True

    assert _gate().accept is True
