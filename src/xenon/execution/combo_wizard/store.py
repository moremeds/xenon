"""Combo wizard store — schema managed by Alembic.

DuckDB table creation removed. init_store() is a backward-compat no-op.
"""

from __future__ import annotations

from pathlib import Path


def init_store(db_path: Path | str | None = None) -> Path:
    """No-op — schema managed by Alembic. Kept for call-site compatibility."""
    return Path(db_path) if db_path else Path("data/orders.duckdb")


def list_tables(db_path: Path | str | None = None) -> set[str]:
    """Return wizard tables in Postgres."""
    try:
        from sqlalchemy import text

        from xenon.db.engine import get_sync_engine

        engine = get_sync_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'xenon' AND table_name LIKE 'wizard_%'"
                )
            ).fetchall()
            return {r[0] for r in rows}
    except Exception:
        return set()
