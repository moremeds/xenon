"""Scope filter for `get_latest_portfolio_payload`.

Phase 1 of the portfolio postgres read-path migration. The single hard
invariant: a query for `live` scope MUST NOT return paper rows, even when
the paper rows are newer or share the same `account` string.

See docs/plans/2026-04-27-portfolio-postgres-read-path.md.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
# Phase 2 carve-out: this module's tests open their own SQLAlchemy engine
# (helpers calling sqlalchemy.create_engine directly, or subprocess CLIs)
# and therefore can't share the test's BEGIN/ROLLBACK transaction. They
# stay on Phase 1 TRUNCATE pre+post isolation via this marker. Migration
# to txn-rollback would require refactoring those local engine helpers to
# go through xenon.db.engine.get_sync_engine().
pytestmark = pytest.mark.committed_db

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from xenon.db.queries.portfolio import get_latest_portfolio_payload
from xenon.db.schema import account_snapshots


def _async_test_db_url() -> str:
    import os

    return os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )


def _seed(rows: list[dict]) -> None:
    async def _go() -> None:
        engine = create_async_engine(_async_test_db_url())
        try:
            async with engine.begin() as conn:
                for row in rows:
                    await conn.execute(insert(account_snapshots).values(**row))
        finally:
            await engine.dispose()

    asyncio.run(_go())


def _fetch(scope: dict) -> dict | None:
    async def _go() -> dict | None:
        engine = create_async_engine(_async_test_db_url())
        try:
            async with engine.connect() as conn:
                return await get_latest_portfolio_payload(conn, **scope)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


PAPER_SCOPE = {"broker": "IB", "account_env": "paper", "broker_account": "DU1234567"}
LIVE_SCOPE = {"broker": "IB", "account_env": "live", "broker_account": "U18007831"}


def _row(scope: dict, *, payload: dict, snapshot_at: datetime) -> dict:
    return {
        "account": scope["broker_account"],
        "bankroll": 0,
        "peak_value": 0,
        "net_liquidation": payload.get("account_summary", {}).get("net_liquidation", 0),
        "payload": payload,
        "snapshot_at": snapshot_at,
        **scope,
    }


def test_returns_none_when_no_snapshot_for_scope():
    _seed([_row(PAPER_SCOPE, payload={"x": 1}, snapshot_at=datetime.now(timezone.utc))])
    assert _fetch(LIVE_SCOPE) is None


def test_returns_latest_payload_for_matching_scope():
    now = datetime.now(timezone.utc)
    _seed(
        [
            _row(LIVE_SCOPE, payload={"version": "old"}, snapshot_at=now - timedelta(minutes=5)),
            _row(LIVE_SCOPE, payload={"version": "new"}, snapshot_at=now),
        ]
    )
    payload = _fetch(LIVE_SCOPE)
    assert payload is not None
    assert payload["version"] == "new"


def test_paper_rows_do_not_leak_into_live_response_even_when_newer():
    """Critical scope-isolation guarantee — would have masked the 2026-04-27 bug
    where a stale paper-mode portfolio.json was being served for a live session.
    """
    now = datetime.now(timezone.utc)
    _seed(
        [
            _row(LIVE_SCOPE, payload={"who": "live"}, snapshot_at=now - timedelta(minutes=10)),
            _row(PAPER_SCOPE, payload={"who": "paper"}, snapshot_at=now),
        ]
    )
    payload = _fetch(LIVE_SCOPE)
    assert payload is not None
    assert payload["who"] == "live"


def test_empty_payload_treated_as_no_snapshot():
    """An `'{}'::jsonb` row (server default) shouldn't be served as a real
    snapshot — return None so the route can 404 instead of returning empty.
    """
    _seed([_row(LIVE_SCOPE, payload={}, snapshot_at=datetime.now(timezone.utc))])
    assert _fetch(LIVE_SCOPE) is None
