"""Signature check for load_account_snapshots_history_sync, plus a sync PG integration test."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import insert, text

from xenon.db.schema import account_snapshots
from xenon.utils.portfolio_loader import load_account_snapshots_history_sync


def test_sync_helper_signature_accepts_scope_and_limit():
    import inspect

    sig = inspect.signature(load_account_snapshots_history_sync)
    params = sig.parameters
    assert "scope" in params
    assert "limit" in params
    assert params["limit"].default == 5


def test_load_account_snapshots_history_sync_returns_desc_limited(pg_test_engine, scope_fixture):
    base = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    with pg_test_engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.account_snapshots CASCADE"))
        for i in range(3):
            conn.execute(
                insert(account_snapshots).values(
                    account=scope_fixture.broker_account,
                    bankroll=Decimal("100000.00"),
                    broker=scope_fixture.broker,
                    account_env=scope_fixture.account_env,
                    broker_account=scope_fixture.broker_account,
                    snapshot_at=base + timedelta(hours=i),
                    payload={"i": i},
                )
            )

    history = load_account_snapshots_history_sync(scope=scope_fixture, limit=2)
    assert len(history) == 2
    assert history[0]["payload"]["i"] == 2
    assert history[1]["payload"]["i"] == 1
