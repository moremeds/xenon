"""Tests for perm_id plumbing through the PG blotter payload (Task 0)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert

from xenon.db.engine import get_sync_engine
from xenon.db.queries.blotter import fetch_blotter_pg
from xenon.db.schema import order_submissions, trades
from xenon.execution.account_scope import AccountScope

_SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU000007")


def _insert_submission(conn, submission_id: str, perm_id: str | None) -> None:
    conn.execute(
        insert(order_submissions).values(
            submission_id=submission_id,
            ticker="AAPL",
            security_type="STK",
            action="BUY",
            quantity=1,
            limit_price=100,
            state="COMPLETED",
            perm_id=perm_id,
            submitted_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
            broker=_SCOPE.broker,
            account_env=_SCOPE.account_env,
            broker_account=_SCOPE.broker_account,
        )
    )


def _insert_trade(conn, *, submission_id: str | None, ticker: str = "AAPL") -> int:
    res = conn.execute(
        insert(trades)
        .values(
            ticker=ticker,
            action="BUY",
            quantity=1,
            entry_cost=100,
            exit_cost=110,
            realized_pnl=10,
            opened_at=datetime(2026, 4, 27, 14, tzinfo=timezone.utc),
            closed_at=datetime(2026, 4, 27, 15, tzinfo=timezone.utc),
            state="CLOSED",
            submission_id=submission_id,
            broker=_SCOPE.broker,
            account_env=_SCOPE.account_env,
            broker_account=_SCOPE.broker_account,
        )
        .returning(trades.c.id)
    )
    return int(res.scalar_one())


def test_payload_includes_perm_id_when_submission_resolves():
    engine = get_sync_engine()
    with engine.begin() as conn:
        _insert_submission(conn, "sub-permid-001", perm_id="PERM-1")
        _insert_trade(conn, submission_id="sub-permid-001")

    with engine.connect() as conn:
        payload = fetch_blotter_pg(conn, scope=_SCOPE, days=30)

    closed = payload["closed_trades"]
    assert any(t.get("perm_id") == "PERM-1" for t in closed)


def test_payload_perm_id_none_when_submission_missing():
    engine = get_sync_engine()
    with engine.begin() as conn:
        _insert_trade(conn, submission_id=None, ticker="MSFT")

    with engine.connect() as conn:
        payload = fetch_blotter_pg(conn, scope=_SCOPE, days=30)

    msft_rows = [t for t in payload["closed_trades"] if t["symbol"] == "MSFT"]
    assert msft_rows
    assert all(t.get("perm_id") is None for t in msft_rows)
