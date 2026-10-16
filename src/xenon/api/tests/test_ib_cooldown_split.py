"""API-6 — IB cooldown is split into data vs order lanes.

A connection failure on a data/quote entry (chain, depth, greeks, sync) must
never put order-mutating entries (place/cancel/modify) into cooldown, and an
order-entry failure must never block data reads.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

from xenon.api import server as server_mod  # noqa: E402
from xenon.api.subprocess import ScriptResult  # noqa: E402


class _ConnectedPool:
    """Pool stand-in that reports every role connected (skips Layer-2 probe)."""

    def is_connected(self, _role: str) -> bool:
        return True


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
