"""Postgres-backed orders_submissions / orders_events store.

Migrated from DuckDB. Preserves the same public API (function signatures,
dataclasses, return types). Combo wizard modules still import _connect_utc /
_resolve_path for their own DuckDB tables — removed in Task 21.

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
from sqlalchemy import create_engine as _create_sync_engine
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.schema import order_events, order_submissions

# ── DuckDB compat kept for combo_wizard (Task 21 removes) ──

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

_MIGRATIONS = [
    "ALTER TABLE orders_submissions ADD COLUMN IF NOT EXISTS modify_sequence INTEGER DEFAULT 0;",
]


def _resolve_path(db_path: Path | str | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    env = os.environ.get("XENON_ORDERS_DB_PATH")
    return Path(env) if env else Path("data/orders.duckdb")


def _connect_utc(path: Path | str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    try:
        con.execute("SET TimeZone='UTC'")
    except duckdb.Error:
        pass
    return con


_WRITE_LOCK = threading.Lock()

# ── Postgres sync engine ──

_pg_engine = None


def _get_pg_engine():
    global _pg_engine
    if _pg_engine is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL not set — no silent fallback post-migration.")
        sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
        _pg_engine = _create_sync_engine(sync_url)
    return _pg_engine


# ── Schema init ──


def init_store(db_path: Path | str | None = None) -> Path:
    """Alembic manages Postgres schema. DuckDB tables kept for combo wizard."""
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
    from xenon.execution.combo_wizard import store as wizard_store

    wizard_store.init_store(path)
    return path


# ── Models ──


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


# ── Core functions (now Postgres-backed) ──


def reserve_attempt(
    user_id: str,
    client_attempt_id: str,
    request: RequestRow,
    db_path: Path | str | None = None,
) -> ReservationOutcome:
    """Atomically reserve a submission slot keyed by (user_id, client_attempt_id)."""
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    engine = _get_pg_engine()
    with engine.begin() as conn:
        stmt = pg_insert(order_submissions).values(
            submission_id=sid,
            user_id=user_id,
            client_attempt_id=client_attempt_id,
            ticker=request.ticker,
            security_type=request.security_type,
            action=request.action,
            quantity=request.quantity,
            expiry=request.expiry,
            strike=request.strike,
            right=request.right,
            multiplier=request.multiplier,
            con_id=request.con_id,
            limit_price=request.limit_price,
            state="PENDING",
            submitted_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_nothing(constraint="uq_order_sub_user_attempt")
        stmt = stmt.returning(order_submissions.c.submission_id)
        inserted = conn.execute(stmt).first()

        if inserted is not None:
            return ReservationOutcome(
                status="winner",
                submission_id=sid,
                state="PENDING",
                duplicate_of=None,
                reason_code=None,
            )

        row = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.state,
                order_submissions.c.ib_order_id,
                order_submissions.c.reason_code,
            ).where(
                order_submissions.c.user_id == user_id,
                order_submissions.c.client_attempt_id == client_attempt_id,
            )
        ).first()
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


def apply_modify(
    order_id: str,
    sequence: int,
    db_path: Path | str | None = None,
) -> dict:
    """Monotonic modify_sequence gate keyed by ib_order_id."""
    engine = _get_pg_engine()
    with engine.begin() as conn:
        result = conn.execute(
            update(order_submissions)
            .where(
                order_submissions.c.ib_order_id == str(order_id),
                order_submissions.c.modify_sequence < sequence,
            )
            .values(
                modify_sequence=sequence,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(order_submissions.c.modify_sequence)
        )
        updated = result.first()
        if updated is not None:
            return {"applied": True, "current_sequence": int(updated[0])}

        row = conn.execute(
            select(order_submissions.c.modify_sequence).where(order_submissions.c.ib_order_id == str(order_id))
        ).first()
        if row is None:
            return {"applied": False, "current_sequence": -1}
        return {"applied": False, "current_sequence": int(row[0])}


def register_from_snapshot(
    perm_id: str,
    ib_order_id: str,
    ticker: str,
    security_type: str,
    action: str,
    quantity: int,
    limit_price: float,
    multiplier: int = 1,
    user_id: str = "snapshot",
    db_path: Path | str | None = None,
) -> bool:
    """Insert a minimal row for an IB order not placed via the FastAPI flow.

    Idempotent: keyed by submission_id = "snapshot-<perm_id>".
    """
    submission_id = f"snapshot-{perm_id}"
    client_attempt_id = f"snapshot-{perm_id}"
    now = datetime.now(timezone.utc)
    engine = _get_pg_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            select(order_submissions.c.submission_id).where(order_submissions.c.submission_id == submission_id)
        ).first()
        if existing is not None:
            return False
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                user_id=user_id,
                client_attempt_id=client_attempt_id,
                ticker=ticker,
                security_type=security_type,
                action=action,
                quantity=quantity,
                multiplier=multiplier,
                ib_order_id=str(ib_order_id),
                perm_id=str(perm_id),
                limit_price=limit_price,
                state="SUBMITTED",
                submitted_at=now,
                updated_at=now,
                modify_sequence=0,
            )
        )
        return True


def apply_modify_by_perm_id(
    perm_id: str,
    sequence: int,
    db_path: Path | str | None = None,
) -> dict:
    """Variant of apply_modify keyed by perm_id."""
    engine = _get_pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(order_submissions.c.ib_order_id).where(order_submissions.c.perm_id == str(perm_id))
        ).first()
    if row is None or not row[0]:
        return {"applied": False, "current_sequence": -1}
    return apply_modify(str(row[0]), sequence, db_path=db_path)


def mark_submitted(
    *,
    submission_id: str,
    ib_order_id: str,
    perm_id: str | None,
    placing_client_id: int | None,
    db_path: Path | str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    engine = _get_pg_engine()
    with engine.begin() as conn:
        conn.execute(
            update(order_submissions)
            .where(order_submissions.c.submission_id == submission_id)
            .values(
                ib_order_id=str(ib_order_id),
                perm_id=str(perm_id) if perm_id is not None else None,
                placing_client_id=placing_client_id,
                state="WORKING",
                updated_at=now,
            )
        )


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
    engine = _get_pg_engine()
    with engine.begin() as conn:
        conn.execute(
            update(order_submissions)
            .where(order_submissions.c.submission_id == submission_id)
            .values(
                state=state,
                reason_code=reason_code,
                filled_qty=filled_qty,
                avg_fill_price=avg_fill_price,
                updated_at=now,
            )
        )


def record_event(
    submission_id: str,
    kind: str,
    detail: dict,
    db_path: Path | str | None = None,
) -> None:
    engine = _get_pg_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_events).values(
                submission_id=submission_id,
                kind=kind,
                detail=detail,
            )
        )


def lookup_submission_id_by_ib_order_id(ib_order_id: str, db_path: Path | str | None = None) -> str | None:
    if not ib_order_id:
        return None
    engine = _get_pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(order_submissions.c.submission_id).where(order_submissions.c.ib_order_id == str(ib_order_id))
        ).first()
    return row[0] if row else None


def lookup_submission_id_by_perm_id(perm_id: str, db_path: Path | str | None = None) -> str | None:
    if not perm_id:
        return None
    engine = _get_pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(order_submissions.c.submission_id).where(order_submissions.c.perm_id == str(perm_id)).limit(1)
        ).first()
    return row[0] if row else None


def lookup_by_attempt(user_id: str, client_attempt_id: str, db_path: Path | str | None = None) -> SubmissionRow | None:
    engine = _get_pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.user_id,
                order_submissions.c.ticker,
                order_submissions.c.state,
                order_submissions.c.ib_order_id,
                order_submissions.c.perm_id,
                order_submissions.c.placing_client_id,
                order_submissions.c.reason_code,
                order_submissions.c.quantity,
                order_submissions.c.action,
                order_submissions.c.security_type,
                order_submissions.c.right,
                order_submissions.c.expiry,
            ).where(
                order_submissions.c.user_id == user_id,
                order_submissions.c.client_attempt_id == client_attempt_id,
            )
        ).first()
    if row is None:
        return None
    vals = list(row)
    if vals[12] is not None and not isinstance(vals[12], str):
        vals[12] = str(vals[12])
    return SubmissionRow(*vals)


from xenon.execution.preflight import WorkingReservations

_ACTIVE_STATES = ("PENDING", "WORKING", "PARTIALLY_FILLED")


def working_reservations_for(user_id: str, ticker: str, db_path: Path | str | None = None) -> WorkingReservations:
    engine = _get_pg_engine()
    with engine.connect() as conn:
        stock_sell = conn.execute(
            select(func.coalesce(func.sum(order_submissions.c.quantity - order_submissions.c.filled_qty), 0)).where(
                order_submissions.c.user_id == user_id,
                order_submissions.c.ticker == ticker,
                order_submissions.c.security_type == "STK",
                order_submissions.c.action == "SELL",
                order_submissions.c.state.in_(_ACTIVE_STATES),
            )
        ).scalar()
        short_call = conn.execute(
            select(func.coalesce(func.sum(order_submissions.c.quantity - order_submissions.c.filled_qty), 0)).where(
                order_submissions.c.user_id == user_id,
                order_submissions.c.ticker == ticker,
                order_submissions.c.security_type == "OPT",
                order_submissions.c.action == "SELL",
                order_submissions.c.right == "C",
                order_submissions.c.state.in_(_ACTIVE_STATES),
            )
        ).scalar()
    return WorkingReservations(
        stock_sell_qty=int(stock_sell),
        short_call_qty=int(short_call),
        short_put_cash_required=Decimal("0"),
        long_call_close_qty_same_exp=0,
    )
