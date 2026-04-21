"""DuckDB-backed orders_submissions / orders_events store.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §12.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

_CREATE_SUBMISSIONS = """
CREATE TABLE IF NOT EXISTS orders_submissions (
    submission_id     TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    client_attempt_id TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    security_type     TEXT NOT NULL,
    action            TEXT NOT NULL,
    quantity          INTEGER NOT NULL,
    expiry            TEXT,
    strike            DECIMAL(18,4),
    "right"           TEXT,
    multiplier        INTEGER NOT NULL,
    con_id            INTEGER,
    placing_client_id INTEGER,
    ib_order_id       TEXT,
    perm_id           TEXT,
    limit_price       DECIMAL(18,4) NOT NULL,
    state             TEXT NOT NULL,
    reason_code       TEXT,
    filled_qty        INTEGER NOT NULL DEFAULT 0,
    avg_fill_price    DECIMAL(18,4),
    submitted_at      TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL,
    UNIQUE (user_id, client_attempt_id)
);
"""

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS orders_events (
    event_id      TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES orders_submissions(submission_id),
    kind          TEXT NOT NULL,
    detail        JSON,
    "at"          TIMESTAMP NOT NULL
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_submissions_state_ticker ON orders_submissions(state, ticker);",
    "CREATE INDEX IF NOT EXISTS ix_submissions_perm_id ON orders_submissions(perm_id);",
    "CREATE INDEX IF NOT EXISTS ix_submissions_ib_order_id ON orders_submissions(ib_order_id);",
    'CREATE INDEX IF NOT EXISTS ix_events_submission ON orders_events(submission_id, "at");',
]


def _resolve_path(db_path: Path | str | None) -> Path:
    if db_path is not None:
        return Path(db_path)
    env = os.environ.get("XENON_ORDERS_DB_PATH")
    return Path(env) if env else Path("data/orders.duckdb")


def init_store(db_path: Path | str | None = None) -> Path:
    path = _resolve_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(_CREATE_SUBMISSIONS)
        con.execute(_CREATE_EVENTS)
        for stmt in _CREATE_INDEXES:
            con.execute(stmt)
    finally:
        con.close()
    return path
