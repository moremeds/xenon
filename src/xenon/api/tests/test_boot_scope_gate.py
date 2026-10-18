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

    async def connect_all(self):
        return {}

    async def disconnect_all(self):
        return None

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
            pass
        return calls

    return _run


async def test_lifespan_skips_boot_writers_when_account_unresolved(boot, caplog):
    calls = await boot("")

    assert calls == []
    assert server_mod.app.state.mode_verified is False
    assert any(r.levelname == "ERROR" and "skipping" in r.getMessage() for r in caplog.records)


async def test_lifespan_runs_boot_writers_with_verified_account(boot):
    account = "DU0000000" if trading_mode.MODE == "paper" else "U0000000"

    calls = await boot(account)

    assert calls == ["rehydrate", "fills_replay", "poller"]
    assert server_mod.app.state.mode_verified is True


async def test_lifespan_skips_boot_writers_when_mode_unverified(boot):
    wrong = "U0000000" if trading_mode.MODE == "paper" else "DU0000000"

    calls = await boot(wrong)

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
