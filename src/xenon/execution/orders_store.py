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
    submission_id TEXT NOT NULL,
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


def _connect_utc(path: Path | str) -> duckdb.DuckDBPyConnection:
    """Connect to DuckDB with the session TimeZone pinned to UTC.

    ``orders_store`` writes ``datetime.now(timezone.utc)``; DuckDB normally
    converts aware values to the local TZ before stripping tzinfo. Pinning
    TimeZone='UTC' keeps the naive wall-clock aligned with UTC, so the
    single_leg_rehydrate reader can safely interpret naive values as UTC.
    """
    con = duckdb.connect(str(path))
    try:
        con.execute("SET TimeZone='UTC'")
    except duckdb.Error:
        # Older DuckDB builds without the ICU extension silently support
        # TimeZone='UTC' but fail on other values; UTC should always work.
        pass
    return con


_MIGRATIONS = [
    # F5.3: monotonic modify_sequence gate. Non-destructive additive migration.
    # DuckDB rejects NOT NULL in ADD COLUMN; DEFAULT 0 backfills existing rows and
    # new inserts omit modify_sequence, so NULLs cannot arise in practice.
    "ALTER TABLE orders_submissions ADD COLUMN IF NOT EXISTS modify_sequence INTEGER DEFAULT 0;",
]


def init_store(db_path: Path | str | None = None) -> Path:
    path = _resolve_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect_utc(path)
    try:
        con.execute(_CREATE_SUBMISSIONS)
        con.execute(_CREATE_EVENTS)
        for stmt in _CREATE_INDEXES:
            con.execute(stmt)
        for stmt in _MIGRATIONS:
            con.execute(stmt)
    finally:
        con.close()
    return path


def apply_modify(
    order_id: str,
    sequence: int,
    db_path: Path | str | None = None,
) -> dict:
    """Monotonic modify_sequence gate keyed by ib_order_id.

    Returns:
        {"applied": True, "current_sequence": <sequence>} if the proposed sequence is
        strictly greater than the stored value (update committed).
        {"applied": False, "current_sequence": <stored>} if the proposed sequence is
        stale (<= stored).
        {"applied": False, "current_sequence": -1} if the ib_order_id is unknown.
        The -1 sentinel lets the route distinguish "not found" (404) from "stale" (409).
    """
    path = _resolve_path(db_path)
    with _WRITE_LOCK:
        con = _connect_utc(path)
        try:
            updated = con.execute(
                """
                UPDATE orders_submissions
                   SET modify_sequence = ?, updated_at = ?
                 WHERE ib_order_id = ? AND modify_sequence < ?
                 RETURNING modify_sequence
                """,
                [sequence, datetime.now(timezone.utc), order_id, sequence],
            ).fetchone()
            if updated is not None:
                return {"applied": True, "current_sequence": int(updated[0])}

            row = con.execute(
                "SELECT modify_sequence FROM orders_submissions WHERE ib_order_id = ?",
                [order_id],
            ).fetchone()
            if row is None:
                return {"applied": False, "current_sequence": -1}
            return {"applied": False, "current_sequence": int(row[0])}
        finally:
            con.close()


def apply_modify_by_perm_id(
    perm_id: str,
    sequence: int,
    db_path: Path | str | None = None,
) -> dict:
    """Variant of apply_modify keyed by perm_id.

    When a modify arrives with only permId (no ib orderId, e.g. because the
    UI only tracks permId), resolve the ib_order_id first, then delegate to
    ``apply_modify``. Matches the same return shape so the route can share the
    same downstream handling.
    """
    path = _resolve_path(db_path)
    con = _connect_utc(path)
    try:
        row = con.execute(
            "SELECT ib_order_id FROM orders_submissions WHERE perm_id = ?",
            [perm_id],
        ).fetchone()
    finally:
        con.close()
    if row is None or not row[0]:
        return {"applied": False, "current_sequence": -1}
    return apply_modify(str(row[0]), sequence, db_path=db_path)


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
        con = _connect_utc(path)
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
                    sid,
                    user_id,
                    client_attempt_id,
                    request.ticker,
                    request.security_type,
                    request.action,
                    request.quantity,
                    request.expiry,
                    str(request.strike) if request.strike is not None else None,
                    request.right,
                    request.multiplier,
                    request.con_id,
                    str(request.limit_price),
                    now,
                    now,
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


@dataclass
class SubmissionRow:
    submission_id: str
    user_id: str
    ticker: str
    state: str
    ib_order_id: str | None
    perm_id: str | None
    placing_client_id: int | None
    reason_code: str | None
    quantity: int
    action: str
    security_type: str
    right: str | None
    expiry: str | None


def mark_submitted(
    *,
    submission_id: str,
    ib_order_id: str,
    perm_id: str | None,
    placing_client_id: int | None,
    db_path: Path | str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with _WRITE_LOCK:
        con = _connect_utc(_resolve_path(db_path))
        try:
            con.execute(
                """
                UPDATE orders_submissions
                   SET ib_order_id = ?, perm_id = ?, placing_client_id = ?,
                       state = 'WORKING', updated_at = ?
                 WHERE submission_id = ?
                """,
                [ib_order_id, perm_id, placing_client_id, now, submission_id],
            )
        finally:
            con.close()


def mark_terminal(
    *,
    submission_id: str,
    state: Literal["FILLED", "REJECTED", "CANCELLED", "FAILED", "PARTIALLY_FILLED"],
    reason_code: str | None,
    filled_qty: int,
    avg_fill_price: Decimal | None,
    db_path: Path | str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with _WRITE_LOCK:
        con = _connect_utc(_resolve_path(db_path))
        try:
            con.execute(
                """
                UPDATE orders_submissions
                   SET state = ?, reason_code = ?, filled_qty = ?,
                       avg_fill_price = ?, updated_at = ?
                 WHERE submission_id = ?
                """,
                [
                    state,
                    reason_code,
                    filled_qty,
                    str(avg_fill_price) if avg_fill_price is not None else None,
                    now,
                    submission_id,
                ],
            )
        finally:
            con.close()


def record_event(
    submission_id: str,
    kind: str,
    detail: dict,
    db_path: Path | str | None = None,
) -> None:
    import json as _json

    eid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with _WRITE_LOCK:
        con = _connect_utc(_resolve_path(db_path))
        try:
            con.execute(
                'INSERT INTO orders_events (event_id, submission_id, kind, detail, "at") VALUES (?, ?, ?, ?, ?)',
                [eid, submission_id, kind, _json.dumps(detail), now],
            )
        finally:
            con.close()


def lookup_submission_id_by_ib_order_id(ib_order_id: str, db_path: Path | str | None = None) -> str | None:
    """Return submission_id for a given ib_order_id, or None if not found.

    Used by the cancel/modify routes to locate the orders_submissions row for
    orders_events attribution. Orders placed before F4 won't have a row — in
    that case the caller skips the event write.
    """
    if not ib_order_id:
        return None
    con = _connect_utc(_resolve_path(db_path))
    try:
        row = con.execute(
            "SELECT submission_id FROM orders_submissions WHERE ib_order_id = ?",
            [ib_order_id],
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def lookup_submission_id_by_perm_id(perm_id: str, db_path: Path | str | None = None) -> str | None:
    """Return submission_id for a given perm_id, or None if not found.

    Mirror of ``lookup_submission_id_by_ib_order_id`` for the permId-only
    modify/cancel path (UI-initiated requests often ship ``orderId=0`` and
    identify the order by permId alone).
    """
    if not perm_id:
        return None
    con = _connect_utc(_resolve_path(db_path))
    try:
        row = con.execute(
            "SELECT submission_id FROM orders_submissions WHERE perm_id = ? LIMIT 1",
            [perm_id],
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def lookup_by_attempt(user_id: str, client_attempt_id: str, db_path: Path | str | None = None) -> SubmissionRow | None:
    con = _connect_utc(_resolve_path(db_path))
    try:
        row = con.execute(
            """
            SELECT submission_id, user_id, ticker, state, ib_order_id, perm_id,
                   placing_client_id, reason_code, quantity, action, security_type,
                   "right", expiry
              FROM orders_submissions
             WHERE user_id = ? AND client_attempt_id = ?
            """,
            [user_id, client_attempt_id],
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return SubmissionRow(*row)


from xenon.execution.preflight import WorkingReservations

_ACTIVE_STATES = ("PENDING", "WORKING", "PARTIALLY_FILLED")


def working_reservations_for(user_id: str, ticker: str, db_path: Path | str | None = None) -> WorkingReservations:
    path = _resolve_path(db_path)
    init_store(path)
    con = _connect_utc(path)
    try:
        stock_sell = con.execute(
            """
            SELECT COALESCE(SUM(quantity - filled_qty), 0)
              FROM orders_submissions
             WHERE user_id = ? AND ticker = ? AND security_type = 'STK'
               AND action = 'SELL' AND state IN ('PENDING', 'WORKING', 'PARTIALLY_FILLED')
            """,
            [user_id, ticker],
        ).fetchone()[0]
        short_call = con.execute(
            """
            SELECT COALESCE(SUM(quantity - filled_qty), 0)
              FROM orders_submissions
             WHERE user_id = ? AND ticker = ? AND security_type = 'OPT'
               AND action = 'SELL' AND "right" = 'C'
               AND state IN ('PENDING', 'WORKING', 'PARTIALLY_FILLED')
            """,
            [user_id, ticker],
        ).fetchone()[0]
    finally:
        con.close()
    return WorkingReservations(
        stock_sell_qty=int(stock_sell),
        short_call_qty=int(short_call),
        short_put_cash_required=Decimal("0"),
        long_call_close_qty_same_exp=0,
    )
