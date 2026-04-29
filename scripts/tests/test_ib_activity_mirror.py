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
