import asyncio

import pytest

import xenon.api.services.ib_activity_mirror as mod
from xenon.execution.account_scope import AccountScope


@pytest.mark.asyncio
async def test_poller_records_heartbeat(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "record_service_health", lambda *a, **k: calls.append((a, k)), raising=True)

    async def fake_runner(fn, **kw):
        return {"open_orders": {}, "fills": {}, "cancel_sweep": {}}

    scope = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")
    task = asyncio.create_task(
        mod.activity_poller_loop(
            ib_client_factory=lambda: None,
            scope=scope,
            interval_s=0.01,
            async_runner=fake_runner,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any(a and a[0] == "ib_activity_poller" for a, _ in calls)
    # liveness heartbeat carries the loop's scope
    _, kw = next((a, k) for a, k in calls if a and a[0] == "ib_activity_poller")
    assert kw.get("broker") == "IB"
    assert kw.get("account_env") == "paper"
