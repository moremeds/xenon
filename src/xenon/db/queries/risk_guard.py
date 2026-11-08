"""Risk guard reads and writes (spec 2026-11-08 §5.2-§5.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.queries.portfolio import DISPLAY_SNAPSHOT
from xenon.db.schema import account_snapshots, risk_events
from xenon.execution.account_scope import AccountScope


async def latest_snapshot(engine: AsyncEngine, scope: AccountScope) -> tuple[dict, datetime] | None:
    """Latest display snapshot payload and its ``snapshot_at`` for ``scope``."""
    stmt = (
        select(account_snapshots.c.payload, account_snapshots.c.snapshot_at)
        .where(account_snapshots.c.broker == scope.broker)
        .where(account_snapshots.c.account_env == scope.account_env)
        .where(account_snapshots.c.broker_account == scope.broker_account)
        .where(DISPLAY_SNAPSHOT)
        .order_by(account_snapshots.c.snapshot_at.desc())
        .limit(1)
    )
    async with engine.connect() as conn:
        row = (await conn.execute(stmt)).first()
    if row is None or not row.payload:
        return None
    return dict(row.payload), row.snapshot_at


async def record_event(
    engine: AsyncEngine,
    scope: AccountScope,
    *,
    incident_key: str,
    incident_id: str,
    kind: str,
    severity: int,
    detail: dict[str, Any],
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(risk_events).values(
                **scope.as_dict(),
                incident_key=incident_key,
                incident_id=incident_id,
                kind=kind,
                severity=severity,
                detail=detail,
            )
        )


async def recent_events(engine: AsyncEngine, scope: AccountScope, limit: int = 50) -> list[dict[str, Any]]:
    stmt = (
        select(risk_events)
        .where(risk_events.c.broker == scope.broker)
        .where(risk_events.c.account_env == scope.account_env)
        .where(risk_events.c.broker_account == scope.broker_account)
        .where(risk_events.c.kind != "ack_state")  # restart state, not an alert
        .order_by(risk_events.c.occurred_at.desc(), risk_events.c.id.desc())
        .limit(limit)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [{**r, "occurred_at": r["occurred_at"].isoformat()} for r in rows]


async def latest_ack_state(engine: AsyncEngine, scope: AccountScope, since: datetime) -> dict[str, Any] | None:
    """``detail`` of the newest ``ack_state`` row of ``scope`` since ``since`` (guard restart restore)."""
    c = risk_events.c
    stmt = (
        select(c.detail)
        .where(c.broker == scope.broker)
        .where(c.account_env == scope.account_env)
        .where(c.broker_account == scope.broker_account)
        .where(c.kind == "ack_state")
        .where(c.occurred_at >= since)
        .order_by(c.id.desc())
        .limit(1)
    )
    async with engine.connect() as conn:
        return (await conn.execute(stmt)).scalar_one_or_none()
