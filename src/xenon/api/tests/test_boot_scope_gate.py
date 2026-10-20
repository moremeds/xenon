"""Boot writers never run without a resolved, verified IB account scope.

PR #198 review (Astra P1-2): when select_ib_account fails,
_get_managed_account_for_health returns "" and mode_verified is False, but
the lifespan still ran the boot rehydrate (which mapped "" to
broker_account=None, i.e. no account filter), the fills replay and the
activity poller. Drives the real (non-test-mode) lifespan with a fake pool.
"""

from __future__ import annotations

import pytest

from xenon.api import server as server_mod
from xenon.api import trading_mode

pytestmark = pytest.mark.asyncio


class _FakePool:
    def __init__(self):
        self.run_sync_calls = []
        self.disconnect_calls = []

    async def connect_all(self):
        return {}

    async def disconnect_all(self, **kwargs):
        self.disconnect_calls.append(kwargs)

    async def run_sync(self, role, fn, *args, **kwargs):
        self.run_sync_calls.append((role, getattr(fn, "__name__", fn), kwargs))
        return {}

    def get_with_reconnect_sync(self, _role):
        return None


class _FakeJournal:
    async def start(self):
        return None

    async def stop(self):
        return None


@pytest.fixture
def boot(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "0")
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    monkeypatch.setattr(server_mod, "validate_auth_config", lambda: "auth posture: test")

    async def _gw():
        return {"port_listening": True}

    async def _noop_async():
        return None

    monkeypatch.setattr(server_mod, "ensure_ib_gateway", _gw)
    monkeypatch.setattr(server_mod, "IBPool", _FakePool)
    monkeypatch.setattr(server_mod.orders_store, "init_store", lambda *a, **k: None)
    monkeypatch.setattr(server_mod, "init_engine", lambda *a, **k: None)
    monkeypatch.setattr(server_mod, "dispose_engine", _noop_async)
    monkeypatch.setattr(server_mod, "_maybe_start_futu_history_loop", lambda: None)
    monkeypatch.setattr(
        "xenon.api.services.journal_auto_import.JournalAutoImportSubscriber", lambda *a, **k: _FakeJournal()
    )
    monkeypatch.setattr(server_mod, "ib_pool", None)

    calls: list[str] = []

    async def _rehydrate():
        calls.append("rehydrate")

    async def _fills():
        calls.append("fills_replay")

    monkeypatch.setattr(server_mod, "_run_rehydrate_on_boot", _rehydrate)
    monkeypatch.setattr(server_mod, "_run_fills_replay_on_boot", _fills)
    monkeypatch.setattr(server_mod, "_maybe_start_activity_poller", lambda: calls.append("poller"))

    async def _run(account: str):
        monkeypatch.setattr(server_mod, "_get_managed_account_for_health", lambda: account)
        async with server_mod.lifespan(server_mod.app):
            calls.append(server_mod.ib_pool)
        return calls

    return _run


async def test_lifespan_skips_boot_writers_when_account_unresolved(boot, caplog):
    calls = [c for c in await boot("") if isinstance(c, str)]

    assert calls == []
    assert server_mod.app.state.mode_verified is False
    assert any(r.levelname == "ERROR" and "skipping" in r.getMessage() for r in caplog.records)


async def test_lifespan_runs_boot_writers_with_verified_account(boot):
    account = "DU0000000" if trading_mode.MODE == "paper" else "U0000000"

    calls = [c for c in await boot(account) if isinstance(c, str)]

    assert calls == ["rehydrate", "fills_replay", "poller"]
    assert server_mod.app.state.mode_verified is True


async def test_lifespan_skips_boot_writers_when_mode_unverified(boot):
    wrong = "U0000000" if trading_mode.MODE == "paper" else "DU0000000"

    calls = [c for c in await boot(wrong) if isinstance(c, str)]

    assert calls == []


# ---------------------------------------------------------------------------
# Helpers fail closed on their own (no None account filter)
# ---------------------------------------------------------------------------


@pytest.fixture
def helper_env(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "0")
    pool = _FakePool()
    monkeypatch.setattr(server_mod, "ib_pool", pool)
    monkeypatch.setattr(server_mod.app.state, "trading_mode", trading_mode.MODE, raising=False)
    monkeypatch.setattr(server_mod.app.state, "mode_verified", True, raising=False)
    monkeypatch.setattr("xenon.db.service_health.record_service_health", lambda *a, **k: None)
    return pool


@pytest.mark.parametrize("account", ["", None])
async def test_rehydrate_helper_returns_early_without_account(helper_env, monkeypatch, account):
    monkeypatch.setattr(server_mod.app.state, "account", account, raising=False)

    await server_mod._run_rehydrate_on_boot()

    assert helper_env.run_sync_calls == []


async def test_rehydrate_helper_filters_by_scope_account(helper_env, monkeypatch):
    monkeypatch.setattr(server_mod.app.state, "account", "DU0000000", raising=False)

    await server_mod._run_rehydrate_on_boot()

    assert helper_env.run_sync_calls
    assert all(kw["broker_account"] == "DU0000000" for _r, _fn, kw in helper_env.run_sync_calls)


async def test_fills_replay_helper_returns_early_without_account(helper_env, monkeypatch):
    monkeypatch.setattr(server_mod.app.state, "account", "", raising=False)

    await server_mod._run_fills_replay_on_boot()

    assert helper_env.run_sync_calls == []


# ---------------------------------------------------------------------------
# Helpers also fail closed on an unverified mode (Astra delta P2)
# ---------------------------------------------------------------------------


def _scope_state(monkeypatch, *, verified: bool):
    monkeypatch.setattr(server_mod.app.state, "account", "DU0000000", raising=False)
    monkeypatch.setattr(server_mod.app.state, "mode_verified", verified, raising=False)


@pytest.mark.parametrize("helper", ["_run_rehydrate_on_boot", "_run_fills_replay_on_boot"])
async def test_boot_helper_returns_early_when_mode_unverified(helper_env, monkeypatch, helper):
    _scope_state(monkeypatch, verified=False)

    await getattr(server_mod, helper)()

    assert helper_env.run_sync_calls == []


@pytest.mark.parametrize("helper", ["_run_rehydrate_on_boot", "_run_fills_replay_on_boot"])
async def test_boot_helper_dispatches_when_verified(helper_env, monkeypatch, helper):
    _scope_state(monkeypatch, verified=True)

    await getattr(server_mod, helper)()

    assert helper_env.run_sync_calls
    assert all(
        kw.get("broker_account", getattr(kw.get("scope"), "broker_account", None)) == "DU0000000"
        for _r, _fn, kw in helper_env.run_sync_calls
    )


@pytest.fixture
def poller_env(helper_env, monkeypatch):
    monkeypatch.delenv("XENON_IB_ACTIVITY_POLLER", raising=False)
    monkeypatch.setattr(server_mod.app.state, "ib_activity_poller_task", None, raising=False)
    started = []

    async def _loop(**kwargs):
        started.append(kwargs["scope"].broker_account)

    monkeypatch.setattr("xenon.api.services.ib_activity_mirror.activity_poller_loop", _loop)
    return started


async def test_activity_poller_not_started_when_mode_unverified(poller_env, monkeypatch):
    _scope_state(monkeypatch, verified=False)

    server_mod._maybe_start_activity_poller()

    assert server_mod.app.state.ib_activity_poller_task is None


async def test_activity_poller_started_when_verified(poller_env, monkeypatch):
    import asyncio

    _scope_state(monkeypatch, verified=True)

    server_mod._maybe_start_activity_poller()

    task = server_mod.app.state.ib_activity_poller_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1)
    assert poller_env == ["DU0000000"]


async def test_lifespan_shutdown_stops_owner_and_disconnects_every_role_bounded(boot, monkeypatch):
    """PR-10 T5: shutdown stops the order owner, then disconnects the pool
    with the bounded budget; a failing owner stop never skips the disconnect."""
    stopped = []

    async def _boom(self):
        stopped.append(True)
        raise RuntimeError("owner stop failed")

    monkeypatch.setattr(server_mod.OrderOwner, "stop", _boom)
    account = "DU0000000" if trading_mode.MODE == "paper" else "U0000000"

    pool = next(c for c in await boot(account) if isinstance(c, _FakePool))

    assert stopped == [True]
    assert pool.disconnect_calls == [{"timeout": server_mod._SHUTDOWN_IB_DISCONNECT_S}]


async def test_hanging_listener_never_blocks_the_ib_disconnect(boot, monkeypatch):
    """Astra T5 F3: a stalled Postgres LISTEN close runs after the IB section
    and is bounded; the pool still gets its disconnect and shutdown ends."""
    import asyncio
    import time

    async def _hang(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(_FakeJournal, "stop", _hang)
    monkeypatch.setitem(server_mod._SHUTDOWN_STEP_S, "listener", 0.1)
    account = "DU0000000" if trading_mode.MODE == "paper" else "U0000000"

    t0 = time.monotonic()
    pool = next(c for c in await boot(account) if isinstance(c, _FakePool))

    assert pool.disconnect_calls == [{"timeout": server_mod._SHUTDOWN_IB_DISCONNECT_S}]
    assert time.monotonic() - t0 < 5
