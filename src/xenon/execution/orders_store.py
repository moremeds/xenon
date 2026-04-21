"""DuckDB-backed orders_submissions / orders_events store.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §12.
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

import duckdb
from pydantic import BaseModel, Field

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


class RequestRow(BaseModel):
    ticker: str
    security_type: Literal["STK", "OPT"]
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    expiry: str | None = None
    strike: Decimal | None = None
    right: Literal["C", "P"] | None = None
    multiplier: int
    con_id: int | None = None
    limit_price: Decimal


@dataclass
class ReservationOutcome:
    status: Literal["winner", "duplicate", "terminal"]
    submission_id: str
    state: str
    duplicate_of: str | None
    reason_code: str | None


_TERMINAL_STATES = {"REJECTED", "CANCELLED", "FAILED"}
_WRITE_LOCK = threading.Lock()


def reserve_attempt(
    user_id: str,
    client_attempt_id: str,
    request: RequestRow,
    db_path: Path | str | None = None,
) -> ReservationOutcome:
    """Atomically reserve a submission slot keyed by (user_id, client_attempt_id)."""
    path = _resolve_path(db_path)
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with _WRITE_LOCK:
        con = duckdb.connect(str(path))
        try:
            inserted = con.execute(
                """
                INSERT INTO orders_submissions (
                    submission_id, user_id, client_attempt_id,
                    ticker, security_type, action, quantity,
                    expiry, strike, "right", multiplier, con_id,
                    limit_price, state, submitted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                ON CONFLICT (user_id, client_attempt_id) DO NOTHING
                RETURNING submission_id;
                """,
                [
                    sid, user_id, client_attempt_id,
                    request.ticker, request.security_type, request.action, request.quantity,
                    request.expiry,
                    str(request.strike) if request.strike is not None else None,
                    request.right, request.multiplier, request.con_id,
                    str(request.limit_price), now, now,
                ],
            ).fetchone()
            if inserted is not None:
                return ReservationOutcome(
                    status="winner",
                    submission_id=sid,
                    state="PENDING",
                    duplicate_of=None,
                    reason_code=None,
                )

            row = con.execute(
                """
                SELECT submission_id, state, ib_order_id, reason_code
                FROM orders_submissions
                WHERE user_id = ? AND client_attempt_id = ?
                """,
                [user_id, client_attempt_id],
            ).fetchone()
            assert row is not None, "ON CONFLICT hit but row not found"
            existing_sid, state, ib_order_id, reason_code = row
            if state in _TERMINAL_STATES:
                return ReservationOutcome(
                    status="terminal",
                    submission_id=existing_sid,
                    state=state,
                    duplicate_of=None,
                    reason_code=reason_code,
                )
            return ReservationOutcome(
                status="duplicate",
                submission_id=existing_sid,
                state=state,
                duplicate_of=ib_order_id,
                reason_code=None,
            )
        finally:
            con.close()
