"""Single read seam for the IB portfolio payload from Postgres.

Phase 2 of the postgres-read-path migration — see
docs/plans/2026-04-27-portfolio-postgres-read-path-phase2.md.

Sync-only on purpose: the FastAPI hot path already has
`xenon.db.queries.portfolio.get_latest_portfolio_payload` (async). This
module exists for sync subprocesses (ib_sync, ib_reconcile,
naked_short_audit), CLI scripts (scanners, ratings, reports), and
non-async services (uw_analyze_candidates).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots
from xenon.execution.account_scope import AccountScope


def load_portfolio_payload_sync(*, scope: AccountScope | None = None) -> dict[str, Any] | None:
    """Return the latest IB portfolio payload from Postgres, or None.

    When `scope` is provided, only rows matching that broker/env/account
    are considered. This is mandatory for safety-critical readers
    (preflight, naked-short audit, reconcile) that must not blend
    paper/live data.

    When `scope` is None, returns the absolute latest snapshot regardless
    of scope. Use only for scope-naive discovery (scanner ticker sets,
    analyst-ratings target list, UW candidate underlyings) where mixing
    accounts is acceptable.
    """
    stmt = select(account_snapshots.c.payload).order_by(account_snapshots.c.snapshot_at.desc()).limit(1)
    if scope is not None:
        stmt = (
            select(account_snapshots.c.payload)
            .where(account_snapshots.c.broker == scope.broker)
            .where(account_snapshots.c.account_env == scope.account_env)
            .where(account_snapshots.c.broker_account == scope.broker_account)
            .order_by(account_snapshots.c.snapshot_at.desc())
            .limit(1)
        )
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(stmt).first()
    if row is None:
        return None
    payload = row.payload or {}
    return dict(payload) if payload else None


def load_account_snapshots_history_sync(*, scope: AccountScope, limit: int = 5) -> list[dict[str, Any]]:
    """Return the most recent N account_snapshots rows for the given scope, desc.

    Sync mirror of `xenon.db.queries.portfolio.get_account_snapshots_history`.
    Used by sync subprocesses (ib_sync entry-date fallback) after the JSON
    cutoff — see the PG migration plan.
    """
    stmt = (
        select(account_snapshots)
        .where(account_snapshots.c.broker == scope.broker)
        .where(account_snapshots.c.account_env == scope.account_env)
        .where(account_snapshots.c.broker_account == scope.broker_account)
        .order_by(account_snapshots.c.snapshot_at.desc())
        .limit(limit)
    )
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(stmt)
        return [dict(row._mapping) for row in result]


def get_portfolio_tickers_sync(*, scope: AccountScope | None = None) -> list[str]:
    """Convenience wrapper: extract unique uppercase tickers from the latest snapshot."""
    payload = load_portfolio_payload_sync(scope=scope)
    if not payload:
        return []
    seen: set[str] = set()
    for pos in payload.get("positions", []):
        ticker = str(pos.get("ticker", "")).upper().strip()
        if ticker:
            seen.add(ticker)
    return sorted(seen)
