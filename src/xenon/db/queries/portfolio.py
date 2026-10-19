from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import account_snapshots


async def get_latest_portfolio_payload(
    conn: AsyncConnection,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
) -> dict | None:
    """Return the most recent structured portfolio payload for the given scope.

    Reads `account_snapshots.payload` (jsonb), which `_save_portfolio_to_postgres`
    populates with the full UI-shaped dict at sync time. Phase 1 of the
    portfolio postgres read-path migration — see
    docs/plans/2026-04-27-portfolio-postgres-read-path.md.
    """
    stmt = (
        select(account_snapshots.c.payload, account_snapshots.c.snapshot_at)
        .where(account_snapshots.c.broker == broker)
        .where(account_snapshots.c.account_env == account_env)
        .where(account_snapshots.c.broker_account == broker_account)
        .order_by(account_snapshots.c.snapshot_at.desc())
        .limit(1)
    )
    result = await conn.execute(stmt)
    row = result.first()
    if row is None:
        return None
    payload = row.payload or {}
    if not payload:
        return None
    return dict(payload)


