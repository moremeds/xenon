"""Postgres-backed watchlist (user preference, scoped by operator user_id="local")."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.engine import get_sync_engine
from xenon.db.schema import user_watchlist


def list_for_user(user_id: str) -> list[dict[str, Any]]:
    stmt = (
        select(
            user_watchlist.c.id,
            user_watchlist.c.symbol,
            user_watchlist.c.sector,
            user_watchlist.c.added_at,
        )
        .where(user_watchlist.c.user_id == user_id)
        .order_by(user_watchlist.c.added_at.desc())
    )
    with get_sync_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt)]


def add(user_id: str, symbol: str, sector: str | None = None) -> None:
    sym = symbol.upper().strip()
    stmt = (
        pg_insert(user_watchlist)
        .values(id=uuid.uuid4().hex, user_id=user_id, symbol=sym, sector=sector)
        .on_conflict_do_nothing(constraint="uq_user_watchlist_user_symbol")
    )
    with get_sync_engine().begin() as conn:
        conn.execute(stmt)


def remove(user_id: str, symbol: str) -> None:
    sym = symbol.upper().strip()
    stmt = delete(user_watchlist).where(user_watchlist.c.user_id == user_id, user_watchlist.c.symbol == sym)
    with get_sync_engine().begin() as conn:
        conn.execute(stmt)
