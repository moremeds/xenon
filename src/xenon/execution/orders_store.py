"""Postgres-backed orders_submissions / orders_events store.

Migrated from DuckDB. Preserves the same public API (function signatures,
dataclasses, return types).

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §12.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.events import CHANNEL_FILL_RECORDED, emit_outbox_in_txn
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_events, order_fills, order_submissions

# ── Schema init ──


def init_store(db_path: Path | str | None = None) -> Path:
    """No-op — schema managed by Alembic. Kept for backward compatibility."""
    return Path(db_path) if db_path is not None else Path("data/orders.duckdb")


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
    *,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> ReservationOutcome:
    """Atomically reserve a submission slot keyed by
    (broker, account_env, broker_account, user_id, client_attempt_id)."""
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
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
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
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
                order_submissions.c.broker == broker,
                order_submissions.c.account_env == account_env,
                order_submissions.c.broker_account == broker_account,
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
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> dict:
    """Monotonic modify_sequence gate keyed by ib_order_id.

    `ib_order_id` is unique only within an IB account session, so when scope
    is provided we filter on it to avoid colliding with rows from a different
    paper/live account.
    """
    scope_conds: list = []
    if broker is not None:
        scope_conds.append(order_submissions.c.broker == broker)
    if account_env is not None:
        scope_conds.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        scope_conds.append(order_submissions.c.broker_account == broker_account)
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(
            update(order_submissions)
            .where(
                order_submissions.c.ib_order_id == str(order_id),
                order_submissions.c.modify_sequence < sequence,
                *scope_conds,
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
            select(order_submissions.c.modify_sequence).where(
                order_submissions.c.ib_order_id == str(order_id),
                *scope_conds,
            )
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
    *,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> bool:
    """Insert a minimal row for an IB order not placed via the FastAPI flow.

    Idempotent: keyed by submission_id = "snapshot-<perm_id>".
    """
    submission_id = f"snapshot-{perm_id}"
    client_attempt_id = f"snapshot-{perm_id}"
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(
            pg_insert(order_submissions)
            .values(
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
                broker=broker,
                account_env=account_env,
                broker_account=broker_account,
            )
            .on_conflict_do_nothing(index_elements=["submission_id"])
            .returning(order_submissions.c.submission_id)
        )
        return result.first() is not None


def apply_modify_by_perm_id(
    perm_id: str,
    sequence: int,
    db_path: Path | str | None = None,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> dict:
    """Variant of apply_modify keyed by perm_id.

    Resolves ib_order_id from perm_id then delegates to apply_modify,
    both within the same engine session to avoid TOCTOU. Scope filters
    isolate paper/live rows that may share a perm_id namespace.
    """
    scope_conds: list = []
    if broker is not None:
        scope_conds.append(order_submissions.c.broker == broker)
    if account_env is not None:
        scope_conds.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        scope_conds.append(order_submissions.c.broker_account == broker_account)
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            select(order_submissions.c.ib_order_id).where(
                order_submissions.c.perm_id == str(perm_id),
                *scope_conds,
            )
        ).first()
        if row is None or not row[0]:
            return {"applied": False, "current_sequence": -1}
        ib_order_id = str(row[0])
        result = conn.execute(
            update(order_submissions)
            .where(
                order_submissions.c.ib_order_id == ib_order_id,
                order_submissions.c.modify_sequence < sequence,
                *scope_conds,
            )
            .values(modify_sequence=sequence, updated_at=datetime.now(timezone.utc))
            .returning(order_submissions.c.modify_sequence)
        )
        updated = result.first()
        if updated is not None:
            return {"applied": True, "current_sequence": int(updated[0])}
        cur = conn.execute(
            select(order_submissions.c.modify_sequence).where(
                order_submissions.c.ib_order_id == ib_order_id,
                *scope_conds,
            )
        ).first()
        return {"applied": False, "current_sequence": int(cur[0]) if cur else -1}


def mark_submitted(
    *,
    submission_id: str,
    ib_order_id: str,
    perm_id: str | None,
    placing_client_id: int | None,
    db_path: Path | str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
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
    engine = get_sync_engine()
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


def record_fill(
    *,
    exec_id: str,
    submission_id: str | None,
    combo_attempt_id: str | None = None,
    perm_id: str | None,
    ib_order_id: str | None = None,
    con_id: int | None,
    ticker: str,
    side: str,
    qty: int,
    price: Decimal,
    commission: Decimal = Decimal(0),
    filled_at: datetime,
    metadata: dict | None = None,
    broker: str,
    account_env: str,
    broker_account: str,
) -> bool:
    """Idempotently record one execution-grain fill and emit fill.recorded."""
    if account_env == "legacy_unknown" or broker_account == "legacy_unknown":
        raise ValueError("record_fill requires explicit account scope")

    engine = get_sync_engine()
    with engine.begin() as conn:
        stmt = (
            pg_insert(order_fills)
            .values(
                exec_id=exec_id,
                submission_id=submission_id,
                combo_attempt_id=combo_attempt_id,
                perm_id=perm_id,
                ib_order_id=str(ib_order_id) if ib_order_id is not None else None,
                con_id=con_id,
                ticker=ticker,
                side=side,
                qty=qty,
                price=price,
                commission=commission,
                filled_at=filled_at,
                metadata=metadata,
                broker=broker,
                account_env=account_env,
                broker_account=broker_account,
            )
            .on_conflict_do_nothing(index_elements=["exec_id"])
            .returning(order_fills.c.exec_id)
        )
        inserted = conn.execute(stmt).first()
        if inserted is None:
            return False

        emit_outbox_in_txn(
            conn,
            channel=CHANNEL_FILL_RECORDED,
            source="record_fill",
            payload={
                "exec_id": exec_id,
                "submission_id": submission_id,
                "combo_attempt_id": combo_attempt_id,
                "perm_id": perm_id,
                "ib_order_id": str(ib_order_id) if ib_order_id is not None else None,
                "con_id": con_id,
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "price": str(price),
                "commission": str(commission),
                "filled_at": filled_at.isoformat(),
                "metadata": metadata,
                "broker": broker,
                "account_env": account_env,
                "broker_account": broker_account,
            },
        )
        return True


def record_event(
    submission_id: str,
    kind: str,
    detail: dict,
    db_path: Path | str | None = None,
) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_events).values(
                submission_id=submission_id,
                kind=kind,
                detail=detail,
            )
        )


def lookup_submission_id_by_ib_order_id(
    ib_order_id: str,
    db_path: Path | str | None = None,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> str | None:
    if not ib_order_id:
        return None
    conditions = [order_submissions.c.ib_order_id == str(ib_order_id)]
    if broker is not None:
        conditions.append(order_submissions.c.broker == broker)
    if account_env is not None:
        conditions.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(order_submissions.c.broker_account == broker_account)
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_submissions.c.submission_id).where(*conditions)).first()
    return row[0] if row else None


def lookup_submission_id_by_perm_id(
    perm_id: str,
    db_path: Path | str | None = None,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> str | None:
    if not perm_id:
        return None
    conditions = [order_submissions.c.perm_id == str(perm_id)]
    if broker is not None:
        conditions.append(order_submissions.c.broker == broker)
    if account_env is not None:
        conditions.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(order_submissions.c.broker_account == broker_account)
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_submissions.c.submission_id).where(*conditions).limit(1)).first()
    return row[0] if row else None


def lookup_by_attempt(
    user_id: str,
    client_attempt_id: str,
    db_path: Path | str | None = None,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> SubmissionRow | None:
    engine = get_sync_engine()
    with engine.connect() as conn:
        conditions = [
            order_submissions.c.user_id == user_id,
            order_submissions.c.client_attempt_id == client_attempt_id,
        ]
        if broker is not None:
            conditions.append(order_submissions.c.broker == broker)
        if account_env is not None:
            conditions.append(order_submissions.c.account_env == account_env)
        if broker_account is not None:
            conditions.append(order_submissions.c.broker_account == broker_account)
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
            ).where(*conditions)
        ).first()
    if row is None:
        return None
    vals = list(row)
    if vals[12] is not None and not isinstance(vals[12], str):
        vals[12] = str(vals[12])
    return SubmissionRow(*vals)


from xenon.execution.preflight import WorkingReservations

_ACTIVE_STATES = ("PENDING", "WORKING", "PARTIALLY_FILLED")


def working_reservations_for(
    user_id: str,
    ticker: str,
    db_path: Path | str | None = None,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> WorkingReservations:
    """Aggregate active working-order quantities for preflight.

    Scope kwargs are critical here: cross-account aggregation produces
    incorrect naked-short / short-call coverage decisions in shared DB.
    """
    scope_conds: list = []
    if broker is not None:
        scope_conds.append(order_submissions.c.broker == broker)
    if account_env is not None:
        scope_conds.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        scope_conds.append(order_submissions.c.broker_account == broker_account)
    engine = get_sync_engine()
    with engine.connect() as conn:
        stock_sell = conn.execute(
            select(func.coalesce(func.sum(order_submissions.c.quantity - order_submissions.c.filled_qty), 0)).where(
                order_submissions.c.user_id == user_id,
                order_submissions.c.ticker == ticker,
                order_submissions.c.security_type == "STK",
                order_submissions.c.action == "SELL",
                order_submissions.c.state.in_(_ACTIVE_STATES),
                *scope_conds,
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
                *scope_conds,
            )
        ).scalar()
    return WorkingReservations(
        stock_sell_qty=int(stock_sell),
        short_call_qty=int(short_call),
        short_put_cash_required=Decimal("0"),
        long_call_close_qty_same_exp=0,
    )
