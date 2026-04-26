"""No-op storage adapter for the deprecated trend scanner."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
DEFAULT_DB_PATH = "data/trend_scan.duckdb"


class _NoopConnection:
    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def close(self) -> None:
        return None


def duckdb_available() -> bool:
    return False


def get_connection(db_path: str = DEFAULT_DB_PATH) -> _NoopConnection:
    logger.debug("trend scan DuckDB storage removed; ignoring db_path=%s", db_path)
    return _NoopConnection()


def init_schema(conn: _NoopConnection) -> None:
    return None


def write_scan_run(conn: _NoopConnection, run: dict[str, Any]) -> None:
    return None


def write_scan_candidates(conn: _NoopConnection, candidates: list[dict[str, Any]]) -> None:
    return None
