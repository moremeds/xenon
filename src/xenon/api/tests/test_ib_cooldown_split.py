"""API-6 — IB cooldown is split into data vs order lanes.

A connection failure on a data/quote entry (chain, depth, greeks, sync) must
never put order-mutating entries (place/cancel/modify) into cooldown, and an
order-entry failure must never block data reads.

OP-1 — after a successful Gateway restart, order entries are never resent
(the first attempt may have reached IB); data entries keep their retry.
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

    async def __call__(self, entry, args, timeout=30):
        self.calls.append(entry)
        return self.results[entry]


@pytest.fixture(autouse=True)
def _fresh_cooldown(monkeypatch):
    monkeypatch.setattr(server_mod, "_ib_last_failure", {"data": 0.0, "order": 0.0})
    monkeypatch.setattr(server_mod, "ib_pool", _ConnectedPool())

    async def _gateway_healthy():
        return {"port_listening": True, "upstream_dead": False}

    monkeypatch.setattr(server_mod, "check_ib_gateway", _gateway_healthy)


async def test_data_failure_does_not_cooldown_order_path(monkeypatch):
    probe = _RunnerProbe(
        {
            "xenon-ib-option-chain": ScriptResult(ok=False, error="Connect call failed: Connection refused"),
            "xenon-ib-place-order": ScriptResult(ok=True, data={"orderId": 1, "permId": 2}),
        }
    )
    monkeypatch.setattr(server_mod, "run_entry_point", probe)

    r1 = await server_mod._run_ib_script_with_recovery("xenon-ib-option-chain", ["--symbol", "SPY"], timeout=5)
    assert not r1.ok
    assert server_mod._ib_last_failure["data"] > 0

    # Place path is a different cooldown lane — it must still reach the runner.
    r2 = await server_mod._run_ib_script_with_recovery("xenon-ib-place-order", ["--json", "{}"], timeout=5)
    assert probe.calls == ["xenon-ib-option-chain", "xenon-ib-place-order"]
    assert r2.ok
    assert r2.data == {"orderId": 1, "permId": 2}


async def test_order_failure_does_not_cooldown_data_path(monkeypatch):
    probe = _RunnerProbe(
        {
            "xenon-ib-order-manage": ScriptResult(ok=False, error="Connect call failed"),
            "xenon-ib-sync": ScriptResult(ok=True, data={}),
        }
    )
    monkeypatch.setattr(server_mod, "run_entry_point", probe)

    r1 = await server_mod._run_ib_script_with_recovery("xenon-ib-order-manage", ["cancel"], timeout=5)
    assert not r1.ok
    assert server_mod._ib_last_failure["order"] > 0

    r2 = await server_mod._run_ib_script_with_recovery("xenon-ib-sync", ["--sync"], timeout=5)
    assert probe.calls == ["xenon-ib-order-manage", "xenon-ib-sync"]
    assert r2.ok


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


async def test_order_lane_cools_down_independently(monkeypatch):
    probe = _RunnerProbe(
        {
            "xenon-ib-order-manage": ScriptResult(ok=False, error="Connect call failed"),
            "xenon-ib-market-depth": ScriptResult(ok=True, data={}),
        }
    )
    monkeypatch.setattr(server_mod, "run_entry_point", probe)

    await server_mod._run_ib_script_with_recovery("xenon-ib-order-manage", ["cancel"], timeout=5)

    # Second order call short-circuits…
    r_order = await server_mod._run_ib_script_with_recovery("xenon-ib-order-manage", ["cancel"], timeout=5)
    assert probe.calls == ["xenon-ib-order-manage"]
    assert "recently failed" in (r_order.error or "")

    # …while a data call proceeds.
    r_data = await server_mod._run_ib_script_with_recovery("xenon-ib-market-depth", [], timeout=5)
    assert r_data.ok
    assert probe.calls == ["xenon-ib-order-manage", "xenon-ib-market-depth"]


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


@pytest.mark.parametrize("entry", ["xenon-ib-place-order", "xenon-ib-order-manage"])
async def test_order_entry_never_retried_after_gateway_restart(monkeypatch, entry):
    """OP-1: a failed order entry may have reached IB — never resend it."""
    pool = _ConnectedPool()
    monkeypatch.setattr(server_mod, "ib_pool", pool)
    restarts = _patch_dead_gateway_restart(monkeypatch)

    calls: list[str] = []

    async def _run(entry, args, timeout=30):
        calls.append(entry)
        return ScriptResult(ok=False, error="Connect call failed: Connection refused")

    monkeypatch.setattr(server_mod, "run_entry_point", _run)

    result = await server_mod._run_ib_script_with_recovery(entry, ["--json", "{}"], timeout=5)

    # run_entry_point called exactly once; pool still reconnected after restart
    # — sync/data only: the orders role belongs to the order owner (A2).
    assert calls == [entry]
    assert restarts["n"] == 1
    assert pool.reconnects == 1
    assert pool.roles == [("sync", "data"), ("sync", "data")]
    assert not result.ok
    assert result.error == (
        "IB Gateway restarted; order request NOT resent — check open orders before retrying"
    )


async def test_data_entry_retries_after_gateway_restart(monkeypatch):
    """Data entries keep the existing retry-once-after-restart behavior."""
    pool = _ConnectedPool()
    monkeypatch.setattr(server_mod, "ib_pool", pool)
    restarts = _patch_dead_gateway_restart(monkeypatch)

    calls: list[str] = []

    async def _run(entry, args, timeout=30):
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
