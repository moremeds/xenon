"""Tests for the IB→Postgres activity mirror service.

Phase 1 (this file): boot-time fills replay. Pulls executions from IB once
on FastAPI startup and inserts them into xenon.order_fills, mirroring the
behavior of the standalone `xenon-ib-reconcile` CLI.

Why: PR #67 made IB→Postgres open-order import authoritative on the open
side. The fill side stayed manual (operator runs `xenon-ib-reconcile`).
Operators forget. The result is fills that exist in IB never reach the
blotter — concretely, a SPX combo that filled in TWS on 2026-04-29 had
zero rows in `xenon.order_fills` because no one ran the CLI.
"""

from __future__ import annotations

import pytest

from xenon.execution.account_scope import AccountScope


@pytest.fixture
def scope() -> AccountScope:
    return AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")


def test_reconcile_fills_on_boot_invokes_record_external_fills(monkeypatch, scope):
    """Happy path: factory returns a client, fetch returns fills, record fires."""
    from xenon.api.services import ib_activity_mirror

    fake_client = object()
    fake_executions = [{"exec_id": "x1", "perm_id": "p1", "symbol": "QQQ", "shares": 1, "price": 5.0}]
    captured: dict = {}

    def fake_fetch(client, lookback_days=7):
        captured["fetch_client"] = client
        return list(fake_executions)

    def fake_record(executions, *, scope):
        captured["record_executions"] = executions
        captured["record_scope"] = scope
        return {"inserted": 1, "replayed": 0, "affected_legacy_ids": []}

    monkeypatch.setattr(ib_activity_mirror, "_fetch_ib_executions", fake_fetch)
    monkeypatch.setattr(ib_activity_mirror, "_record_external_fills", fake_record)

    result = ib_activity_mirror.reconcile_fills_on_boot(
        ib_client_factory=lambda: fake_client,
        scope=scope,
    )

    assert result["inserted"] == 1
    assert captured["fetch_client"] is fake_client
    assert captured["record_scope"] == scope
    assert captured["record_executions"] == fake_executions


def test_reconcile_fills_on_boot_returns_skipped_when_factory_raises(monkeypatch, scope):
    """If the IB pool's sync client isn't available (test mode, gateway down),
    the factory raises. We must not propagate — boot replay is best-effort."""
    from xenon.api.services import ib_activity_mirror

    def boom():
        raise RuntimeError("ib_pool sync role has no client")

    record_called = False

    def fake_record(executions, *, scope):  # pragma: no cover — must not run
        nonlocal record_called
        record_called = True
        return {}

    monkeypatch.setattr(ib_activity_mirror, "_record_external_fills", fake_record)

    result = ib_activity_mirror.reconcile_fills_on_boot(
        ib_client_factory=boom,
        scope=scope,
    )

    assert result["skipped"] is True
    assert "ib_pool" in result.get("reason", "")
    assert record_called is False


def test_reconcile_fills_on_boot_returns_error_when_fetch_raises(monkeypatch, scope):
    """A timeout or RPC failure inside fetch_ib_executions must not crash boot."""
    from xenon.api.services import ib_activity_mirror

    def fake_fetch(client, lookback_days=7):
        raise TimeoutError("IB request timed out")

    record_called = False

    def fake_record(executions, *, scope):  # pragma: no cover
        nonlocal record_called
        record_called = True
        return {}

    monkeypatch.setattr(ib_activity_mirror, "_fetch_ib_executions", fake_fetch)
    monkeypatch.setattr(ib_activity_mirror, "_record_external_fills", fake_record)

    result = ib_activity_mirror.reconcile_fills_on_boot(
        ib_client_factory=lambda: object(),
        scope=scope,
    )

    assert result["error"] == "IB request timed out"
    assert record_called is False


def test_reconcile_fills_on_boot_returns_zero_when_no_executions(monkeypatch, scope):
    """Empty fill list is a valid steady state — return zero counts cleanly."""
    from xenon.api.services import ib_activity_mirror

    monkeypatch.setattr(ib_activity_mirror, "_fetch_ib_executions", lambda c, lookback_days=7: [])
    record_called = False

    def fake_record(executions, *, scope):
        nonlocal record_called
        record_called = True
        return {"inserted": 0, "replayed": 0, "affected_legacy_ids": []}

    monkeypatch.setattr(ib_activity_mirror, "_record_external_fills", fake_record)

    result = ib_activity_mirror.reconcile_fills_on_boot(
        ib_client_factory=lambda: object(),
        scope=scope,
    )

    assert result == {"inserted": 0, "replayed": 0, "affected_legacy_ids": []}
    assert record_called is True


def test_lifespan_helper_skips_in_test_mode(monkeypatch):
    """The server-side wrapper short-circuits when XENON_API_TEST_MODE=1.

    Stops the test suite from hitting real IB, even if a test forgets to
    monkeypatch the IB pool. The unit tests above cover the function-level
    contract; this one locks in the lifespan-side guard.
    """
    import asyncio

    from xenon.api import server

    called = False

    def fake_reconcile(**kwargs):  # pragma: no cover — must not run
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("xenon.api.services.ib_activity_mirror.reconcile_fills_on_boot", fake_reconcile)
    monkeypatch.setattr(server, "_is_test_mode", lambda: True)

    asyncio.run(server._run_fills_replay_on_boot())
    assert called is False


def test_lifespan_helper_skips_when_scope_unresolved(monkeypatch):
    """If app.state.trading_mode/account aren't set yet, skip instead of
    constructing an invalid AccountScope. Matches the rehydrate behavior."""
    import asyncio
    from types import SimpleNamespace

    from xenon.api import server

    called = False

    def fake_reconcile(**kwargs):  # pragma: no cover — must not run
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("xenon.api.services.ib_activity_mirror.reconcile_fills_on_boot", fake_reconcile)
    monkeypatch.setattr(server, "_is_test_mode", lambda: False)
    monkeypatch.setattr(server, "app", SimpleNamespace(state=SimpleNamespace()))

    asyncio.run(server._run_fills_replay_on_boot())
    assert called is False


# ---------------------------------------------------------------------------
# Periodic poller — runs forever, ticks both surfaces (open orders + fills).
# Gated on XENON_IB_ACTIVITY_POLLER so the feature lands without surprise.
# ---------------------------------------------------------------------------


def test_run_activity_poll_tick_calls_both_surfaces(monkeypatch, scope):
    """One tick must call sync_open_orders_to_postgres AND record_external_fills.

    The whole point of this PR is that those two surfaces stay in step. Either
    one alone leaves us in the same broken state we started from.
    """
    from xenon.api.services import ib_activity_mirror

    fake_client = object()
    captured: dict = {}

    monkeypatch.setattr(
        ib_activity_mirror,
        "_fetch_open_orders",
        lambda c: [{"orderId": 1, "permId": 9, "contract": {"secType": "STK", "symbol": "QQQ"}}],
    )
    monkeypatch.setattr(
        ib_activity_mirror,
        "_fetch_ib_executions",
        lambda c, lookback_days=7: [{"exec_id": "x1", "perm_id": "9", "symbol": "QQQ"}],
    )

    def fake_sync(open_orders, *, scope):
        captured["sync_orders"] = open_orders
        captured["sync_scope"] = scope
        return {"registered": 1, "updated": 0, "skipped": 0, "open_count": 1}

    def fake_record(executions, *, scope):
        captured["record_executions"] = executions
        captured["record_scope"] = scope
        return {"inserted": 1, "replayed": 0, "affected_legacy_ids": [], "affected_submission_ids": []}

    monkeypatch.setattr(ib_activity_mirror, "_sync_open_orders_to_postgres", fake_sync)
    monkeypatch.setattr(ib_activity_mirror, "_record_external_fills", fake_record)

    result = ib_activity_mirror.run_activity_poll_tick(
        ib_client_factory=lambda: fake_client,
        scope=scope,
    )

    assert captured["sync_scope"] == scope
    assert captured["record_scope"] == scope
    assert result["open_orders"]["registered"] == 1
    assert result["fills"]["inserted"] == 1


def test_run_activity_poll_tick_swallows_each_surface_independently(monkeypatch, scope):
    """If the open-order side fails, the fills side must still run, and vice
    versa. One transient IB hiccup must not lose a whole poll cycle."""
    from xenon.api.services import ib_activity_mirror

    monkeypatch.setattr(
        ib_activity_mirror,
        "_fetch_open_orders",
        lambda c: (_ for _ in ()).throw(RuntimeError("ib timeout")),
    )
    monkeypatch.setattr(ib_activity_mirror, "_fetch_ib_executions", lambda c, lookback_days=7: [])
    fills_ran = False

    def fake_record(executions, *, scope):
        nonlocal fills_ran
        fills_ran = True
        return {"inserted": 0, "replayed": 0, "affected_legacy_ids": [], "affected_submission_ids": []}

    monkeypatch.setattr(ib_activity_mirror, "_record_external_fills", fake_record)

    result = ib_activity_mirror.run_activity_poll_tick(
        ib_client_factory=lambda: object(),
        scope=scope,
    )

    assert "error" in result["open_orders"]
    assert fills_ran is True
    assert result["fills"]["inserted"] == 0


def test_poller_tick_returns_updated_count(monkeypatch, scope, caplog):
    """Late commission updates are a first-class fill-side outcome."""
    import asyncio
    import logging

    from xenon.api.services import ib_activity_mirror

    monkeypatch.setattr(
        ib_activity_mirror,
        "_fetch_open_orders",
        lambda c: [{"orderId": 1, "permId": 9, "contract": {"secType": "STK", "symbol": "QQQ"}}],
    )
    monkeypatch.setattr(
        ib_activity_mirror,
        "_fetch_ib_executions",
        lambda c, lookback_days=7: [{"exec_id": "x1", "perm_id": "9", "symbol": "QQQ"}],
    )
    monkeypatch.setattr(
        ib_activity_mirror,
        "_sync_open_orders_to_postgres",
        lambda open_orders, *, scope: {"registered": 0, "updated": 0, "skipped": 0, "open_count": 1},
    )
    monkeypatch.setattr(
        ib_activity_mirror,
        "_record_external_fills",
        lambda executions, *, scope: {
            "inserted": 0,
            "updated": 1,
            "replayed": 0,
            "affected_legacy_ids": [],
            "affected_submission_ids": [],
        },
    )

    result = ib_activity_mirror.run_activity_poll_tick(
        ib_client_factory=lambda: object(),
        scope=scope,
    )
    assert result["fills"]["updated"] == 1

    tick_count = 0

    def fake_tick(**kwargs):
        nonlocal tick_count
        tick_count += 1
        return result

    monkeypatch.setattr(ib_activity_mirror, "run_activity_poll_tick", fake_tick)

    async def _run_once():
        task = asyncio.create_task(
            ib_activity_mirror.activity_poller_loop(
                ib_client_factory=lambda: object(),
                scope=scope,
                interval_s=60,
            )
        )
        while tick_count == 0:
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level(logging.INFO, logger="xenon.api.services.ib_activity_mirror"):
        asyncio.run(_run_once())

    assert "fills[ins=0 upd=1 rep=0]" in caplog.text


def test_activity_poller_loop_runs_until_cancelled(monkeypatch, scope):
    """The forever loop should call run_activity_poll_tick repeatedly and
    exit cleanly on asyncio.CancelledError."""
    import asyncio

    from xenon.api.services import ib_activity_mirror

    tick_count = 0

    def fake_tick(**kwargs):
        nonlocal tick_count
        tick_count += 1
        return {"open_orders": {}, "fills": {}}

    monkeypatch.setattr(ib_activity_mirror, "run_activity_poll_tick", fake_tick)

    async def _run():
        task = asyncio.create_task(
            ib_activity_mirror.activity_poller_loop(
                ib_client_factory=lambda: object(),
                scope=scope,
                interval_s=0.01,  # fast for tests; loop sleeps between ticks
            )
        )
        # Let a few ticks fire, then cancel.
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert tick_count >= 2


def test_activity_poller_loop_recovers_from_tick_failure(monkeypatch, scope):
    """A raise inside run_activity_poll_tick must not kill the loop."""
    import asyncio

    from xenon.api.services import ib_activity_mirror

    calls = 0

    def fake_tick(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        return {"open_orders": {}, "fills": {}}

    monkeypatch.setattr(ib_activity_mirror, "run_activity_poll_tick", fake_tick)

    async def _run():
        task = asyncio.create_task(
            ib_activity_mirror.activity_poller_loop(
                ib_client_factory=lambda: object(),
                scope=scope,
                interval_s=0.01,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert calls >= 2  # proves we kept ticking after the first one threw
