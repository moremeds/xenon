"""API-6 — IB cooldown for data entry points.

Since PR-10 order writes never spawn (they run on the order owner), so the
order lane is gone; the data lane keeps its cooldown and its retry after a
Gateway restart.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

from xenon.api import server as server_mod  # noqa: E402
from xenon.api.subprocess import ScriptResult  # noqa: E402


class _ConnectedPool:
    """Pool stand-in that reports every role connected (skips Layer-2 probe)."""

    def __init__(self) -> None:
        self.reconnects = 0
        self.roles: list = []

    def is_connected(self, _role: str) -> bool:
        return True

    async def disconnect_all(self, roles=None) -> None:
        self.reconnects += 1
        self.roles.append(roles)

    async def connect_all(self, roles=None) -> None:
        self.roles.append(roles)


class _RunnerProbe:
    """run_entry_point stand-in returning per-entry results and logging calls."""

    def __init__(self, results: dict[str, ScriptResult]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def __call__(self, entry, args, timeout=30, env=None):
        self.calls.append(entry)
        return self.results[entry]


@pytest.fixture(autouse=True)
def _fresh_cooldown(monkeypatch):
    monkeypatch.setattr(server_mod, "_ib_last_failure", {"data": 0.0})
    monkeypatch.setattr(server_mod, "ib_pool", _ConnectedPool())

    async def _gateway_healthy():
        return {"port_listening": True, "upstream_dead": False}

    monkeypatch.setattr(server_mod, "check_ib_gateway", _gateway_healthy)


async def test_same_lane_failure_still_cools_down(monkeypatch):
    """The split must not remove the cooldown itself — same lane still fast-fails."""
    probe = _RunnerProbe(
        {"xenon-ib-option-chain": ScriptResult(ok=False, error="Connect call failed")}
    )
    monkeypatch.setattr(server_mod, "run_entry_point", probe)

    r1 = await server_mod._run_ib_script_with_recovery("xenon-ib-option-chain", [], timeout=5)
    assert not r1.ok

    r2 = await server_mod._run_ib_script_with_recovery("xenon-ib-option-chain", [], timeout=5)
    # Second data call inside the window never reached the runner.
    assert probe.calls == ["xenon-ib-option-chain"]
    assert "recently failed" in (r2.error or "")


def _patch_dead_gateway_restart(monkeypatch):
    """Route the recovery path into a successful local gateway restart."""

    async def _gateway_dead():
        return {"port_listening": False, "upstream_dead": False}

    monkeypatch.setattr(server_mod, "check_ib_gateway", _gateway_dead)
    monkeypatch.setattr(server_mod, "is_cloud_mode", lambda: False)
    monkeypatch.setattr(server_mod, "is_docker_mode", lambda: False)

    restarts = {"n": 0}

    async def _restart():
        restarts["n"] += 1
        return {"restarted": True, "port_listening": True}

    monkeypatch.setattr(server_mod, "restart_ib_gateway", _restart)
    return restarts


async def test_data_entry_retries_after_gateway_restart(monkeypatch):
    """Data entries keep the existing retry-once-after-restart behavior."""
    pool = _ConnectedPool()
    monkeypatch.setattr(server_mod, "ib_pool", pool)
    restarts = _patch_dead_gateway_restart(monkeypatch)

    calls: list[str] = []

    async def _run(entry, args, timeout=30, env=None):
        calls.append(entry)
        if len(calls) == 1:
            return ScriptResult(ok=False, error="Connect call failed")
        return ScriptResult(ok=True, data={"positions": []})

    monkeypatch.setattr(server_mod, "run_entry_point", _run)

    result = await server_mod._run_ib_script_with_recovery("xenon-ib-sync", ["--sync"], timeout=5)

    assert calls == ["xenon-ib-sync", "xenon-ib-sync"]
    assert restarts["n"] == 1
    assert pool.reconnects == 1
    assert result.ok
    assert result.data == {"positions": []}
