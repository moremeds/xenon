from __future__ import annotations

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
