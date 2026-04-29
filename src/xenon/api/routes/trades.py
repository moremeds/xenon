"""Trade read endpoints backed by Postgres."""

from __future__ import annotations

from datetime import timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from xenon.api.guards import get_account_scope
from xenon.db.engine import get_sync_engine
from xenon.db.schema import trades
from xenon.execution.account_scope import AccountScope

router = APIRouter()


def _iso_utc(value) -> str:
    return value.astimezone(timezone.utc).isoformat()


@router.get("/trades/entry-dates")
async def trades_entry_dates(scope: AccountScope = Depends(get_account_scope)) -> dict[str, str]:
    """Return ticker -> earliest opened_at timestamp for the active account."""
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(trades.c.ticker, func.min(trades.c.opened_at).label("first_open"))
            .where(
                trades.c.broker == scope.broker,
                trades.c.account_env == scope.account_env,
                trades.c.broker_account == scope.broker_account,
                trades.c.opened_at.is_not(None),
            )
            .group_by(trades.c.ticker)
        ).all()
    return {
        row._mapping["ticker"]: _iso_utc(row._mapping["first_open"])
        for row in rows
        if row._mapping["first_open"] is not None
    }
