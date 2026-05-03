"""Async PG-backed tests for get_account_snapshots_history.

Lives here (not in scripts/tests/) because the async `conn` fixture is defined
in src/xenon/db/tests/conftest.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import insert

from xenon.db.queries.portfolio import get_account_snapshots_history
from xenon.db.schema import account_snapshots
from xenon.execution.account_scope import AccountScope


@pytest.mark.asyncio
async def test_get_account_snapshots_history_returns_desc_limited(conn, scope_fixture):
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        await conn.execute(
            insert(account_snapshots).values(
                account=scope_fixture.broker_account,
                bankroll=Decimal("100000.00"),
                broker=scope_fixture.broker,
                account_env=scope_fixture.account_env,
                broker_account=scope_fixture.broker_account,
                snapshot_at=base + timedelta(hours=i),
                payload={"positions": [{"ticker": f"T{i}"}]},
            )
        )

    history = await get_account_snapshots_history(
        conn,
        broker=scope_fixture.broker,
        account_env=scope_fixture.account_env,
        broker_account=scope_fixture.broker_account,
        limit=2,
    )

    assert len(history) == 2
    assert history[0]["payload"]["positions"][0]["ticker"] == "T2"
    assert history[1]["payload"]["positions"][0]["ticker"] == "T1"
    assert history[0]["snapshot_at"] > history[1]["snapshot_at"]


@pytest.mark.asyncio
async def test_get_account_snapshots_history_filters_by_scope(conn, scope_fixture):
    other_scope = AccountScope(broker="IB", account_env="live", broker_account="U1234567")
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    for sc, label in [(scope_fixture, "paper"), (other_scope, "live")]:
        await conn.execute(
            insert(account_snapshots).values(
                account=sc.broker_account,
                bankroll=Decimal("100000.00"),
                broker=sc.broker,
                account_env=sc.account_env,
                broker_account=sc.broker_account,
                snapshot_at=base,
                payload={"label": label},
            )
        )

    history = await get_account_snapshots_history(
        conn,
        broker=scope_fixture.broker,
        account_env=scope_fixture.account_env,
        broker_account=scope_fixture.broker_account,
        limit=10,
    )
    assert len(history) == 1
    assert history[0]["payload"]["label"] == "paper"
