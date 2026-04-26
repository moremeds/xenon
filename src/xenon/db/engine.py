from __future__ import annotations

import os
import re

from sqlalchemy import Engine
from sqlalchemy import create_engine as _create_sync_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_engine: AsyncEngine | None = None
_sync_engine: Engine | None = None


def _normalize_pg_url(url: str, *, driver: str = "asyncpg") -> str:
    """Normalize any postgresql:// URL to the requested driver dialect."""
    base = re.sub(r"^postgresql(\+\w+)?://", "postgresql://", url)
    if driver == "asyncpg":
        return base.replace("postgresql://", "postgresql+asyncpg://", 1)
    if driver == "psycopg":
        return base.replace("postgresql://", "postgresql+psycopg://", 1)
    return base


def create_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    resolved = url or os.environ.get("DATABASE_URL")
    if not resolved:
        raise RuntimeError("DATABASE_URL not set and no url provided")
    async_url = _normalize_pg_url(resolved, driver="asyncpg")
    defaults = {
        "pool_size": 10,
        "max_overflow": 5,
        "pool_pre_ping": True,
        "pool_recycle": 3600,
    }
    defaults.update(kwargs)
    return create_async_engine(async_url, **defaults)


def init_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    global _engine
    _engine = create_engine(url, **kwargs)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Engine not initialized — call init_engine() first")
    return _engine


async def dispose_engine() -> None:
    global _engine, _sync_engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
    if _sync_engine is not None:
        _sync_engine.dispose()
        _sync_engine = None


def get_sync_engine() -> Engine:
    global _sync_engine
    if _sync_engine is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL not set — no silent fallback post-migration.")
        sync_url = _normalize_pg_url(url, driver="psycopg")
        _sync_engine = _create_sync_engine(sync_url, pool_pre_ping=True)
    return _sync_engine
