from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import wizard_events, wizard_sessions


async def create_session(
    conn: AsyncConnection,
    *,
    session_id: str,
    ticker: str,
    state: str,
    structure_name: str | None = None,
    intent: str | None = None,
    payload: dict | None = None,
    current_attempt_id: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    await conn.execute(
        insert(wizard_sessions).values(
            session_id=session_id,
            ticker=ticker,
            state=state,
            structure_name=structure_name,
            intent=intent,
            payload=payload,
            current_attempt_id=current_attempt_id,
            created_at=now,
            updated_at=now,
        )
    )


async def get_session(conn: AsyncConnection, session_id: str) -> dict | None:
    stmt = select(wizard_sessions).where(wizard_sessions.c.session_id == session_id)
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def update_session_state(
    conn: AsyncConnection,
    *,
    session_id: str,
    state: str,
    payload: dict | None = None,
    current_attempt_id: str | None = None,
) -> None:
    values: dict = {
        "state": state,
        "updated_at": datetime.now(timezone.utc),
    }
    if payload is not None:
        values["payload"] = payload
    if current_attempt_id is not None:
        values["current_attempt_id"] = current_attempt_id
    await conn.execute(update(wizard_sessions).where(wizard_sessions.c.session_id == session_id).values(**values))


async def record_event(
    conn: AsyncConnection,
    *,
    session_id: str,
    kind: str,
    detail: dict | None = None,
) -> None:
    await conn.execute(insert(wizard_events).values(session_id=session_id, kind=kind, detail=detail))


async def get_events(conn: AsyncConnection, *, session_id: str) -> list[dict]:
    stmt = select(wizard_events).where(wizard_events.c.session_id == session_id).order_by(wizard_events.c.at)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
