from __future__ import annotations

import importlib
from unittest.mock import patch

from xenon.execution.account_scope import AccountScope
from xenon.monitor_daemon import run as run_mod


def test_position_rules_disabled_by_default(monkeypatch):
    monkeypatch.delenv("XENON_POSITION_RULES_ENABLED", raising=False)
    with patch.object(run_mod.MonitorDaemon, "load_state", lambda self: None):
        daemon = run_mod.create_daemon()

    handler_names = [handler.name for handler in daemon.handlers]
    assert "position_rules" not in handler_names
    assert "wizard_stop_monitor" not in handler_names
    assert daemon.async_tasks == []


def test_position_rules_enabled_when_flag_set(monkeypatch):
    monkeypatch.setenv("XENON_POSITION_RULES_ENABLED", "1")
    scope = AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")

    with (
        patch.object(run_mod.MonitorDaemon, "load_state", lambda self: None),
        patch.object(run_mod, "resolve_from_env", return_value=scope),
        patch.object(run_mod, "get_sync_engine", return_value=object()),
        patch.object(run_mod, "IBClient", return_value=object()),
        patch.object(run_mod, "IBExecutor", return_value=object()),
    ):
        daemon = run_mod.create_daemon()

    handler_names = [handler.name for handler in daemon.handlers]
    assert "position_rules" in handler_names
    assert len(daemon.async_tasks) == 1


def test_position_rules_startup_runs_boot_reconcile_after_connect(monkeypatch):
    monkeypatch.setenv("XENON_POSITION_RULES_ENABLED", "1")
    monkeypatch.setenv("IB_GATEWAY_HOST", "127.0.0.1")
    monkeypatch.setenv("IB_GATEWAY_PORT", "4002")
    monkeypatch.setenv("XENON_POSITION_RULES_CLIENT_ID", "71")
    scope = AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")
    engine = object()
    calls: list[tuple[str, object]] = []

    class FakeIBClient:
        def connect(self, *, host, port, client_id):
            calls.append(("connect", {"host": host, "port": port, "client_id": client_id}))

    def fake_boot_reconcile(*, engine, ib_client, scope):
        calls.append(("boot_reconcile", {"engine": engine, "ib_client": ib_client, "scope": scope}))
        return {"status": "ok"}

    monkeypatch.setattr(run_mod, "boot_reconcile", fake_boot_reconcile, raising=False)
    with (
        patch.object(run_mod.MonitorDaemon, "load_state", lambda self: None),
        patch.object(run_mod, "resolve_from_env", return_value=scope),
        patch.object(run_mod, "get_sync_engine", return_value=engine),
        patch.object(run_mod, "IBClient", return_value=FakeIBClient()),
        patch.object(run_mod, "IBExecutor", return_value=object()),
    ):
        run_mod.create_daemon()

    assert [name for name, _ in calls] == ["connect", "boot_reconcile"]
    assert calls[0][1] == {"host": "127.0.0.1", "port": 4002, "client_id": 71}
    assert calls[1][1]["engine"] is engine
    assert calls[1][1]["scope"] is scope


def test_fill_monitor_uses_paper_gateway_port_in_paper_mode(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")

    import xenon.api.trading_mode as trading_mode
    import xenon.clients.ib_client as ib_client
    import xenon.monitor_daemon.handlers.fill_monitor as fill_monitor
    import xenon.monitor_daemon.run as daemon_run

    importlib.reload(trading_mode)
    importlib.reload(ib_client)
    importlib.reload(fill_monitor)
    daemon_run = importlib.reload(daemon_run)

    with patch.object(daemon_run.MonitorDaemon, "load_state", lambda self: None):
        daemon = daemon_run.create_daemon()

    fill_handler = next(handler for handler in daemon.handlers if handler.name == "fill_monitor")
    assert fill_handler.ib_port == 4002
