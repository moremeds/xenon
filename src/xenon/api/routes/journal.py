"""Journal endpoints backed by Postgres."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from xenon.api.guards import get_account_scope
from xenon.db.engine import get_sync_engine
from xenon.db.queries.journal import create_journal_entry, list_journal_entries, resolve_trade_ticker
from xenon.execution.account_scope import AccountScope

router = APIRouter()


def _parse_authored_at(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise HTTPException(status_code=400, detail="authored_at must be an ISO timestamp")


@router.get("/journal")
async def journal_list(
    scope: AccountScope = Depends(get_account_scope),
    days: int = Query(90, ge=1, le=3650),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, list[dict[str, Any]]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    engine = get_sync_engine()
    with engine.connect() as conn:
        trades = list_journal_entries(conn, scope=scope, cutoff=cutoff, limit=limit)
    return {"trades": trades}


@router.post("/journal")
async def journal_create(
    body: dict[str, Any],
    scope: AccountScope = Depends(get_account_scope),
) -> dict[str, Any]:
    trade_id_raw = body.get("trade_id")
    trade_id = int(trade_id_raw) if trade_id_raw not in (None, "") else None
    ticker = str(body.get("ticker") or "").strip().upper()

    engine = get_sync_engine()
    with engine.begin() as conn:
        if not ticker and trade_id is not None:
            ticker = resolve_trade_ticker(conn, trade_id=trade_id, scope=scope) or ""
        if not ticker:
            raise HTTPException(status_code=400, detail="ticker or trade_id is required")

        metadata = body.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise HTTPException(status_code=400, detail="metadata must be an object")

        return create_journal_entry(
            conn,
            scope=scope,
            ticker=ticker,
            trade_id=trade_id,
            decision=body.get("decision"),
            note=body.get("note"),
            attachments=body.get("attachments"),
            authored_by=body.get("authored_by"),
            authored_at=_parse_authored_at(body.get("authored_at")),
            metadata=metadata,
        )
