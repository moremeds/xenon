"""Postgres-backed orders_submissions / orders_events store.

Migrated from DuckDB. Preserves the same public API (function signatures,
dataclasses, return types).

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §12.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import Text, cast, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.engine import get_sync_engine
from xenon.db.events import CHANNEL_FILL_COMMISSION_UPDATED, CHANNEL_FILL_RECORDED, emit_outbox_in_txn
from xenon.db.schema import order_events, order_fills, order_submissions

# ── Schema init ──


def init_store(*_args, **_kwargs) -> None:
    """No-op — schema is managed by Alembic. Kept as a callable for legacy
    tests; any positional/keyword arguments (e.g. legacy ``db_path=``) are
    silently ignored.
    """
    return None


# ── Models ──


class RequestRow(BaseModel):
    ticker: str
    security_type: Literal["STK", "OPT", "BAG"]
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
    *,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
    strike: float | None = None,
    right: str | None = None,
    expiry: str | None = None,
    con_id: int | None = None,
    tif: str = "DAY",
) -> dict:
    """Mirror an IB-side open order into `xenon.order_submissions`.

    Three branches, one transaction:

    1. **No existing row** — INSERT a `snapshot-<perm_id>` row in state
       `WORKING`. Returns ``{"action": "INSERTED", "drift": None}``.
    2. **Existing snapshot-* row** — compare ``limit_price`` and ``quantity``
       against incoming. On drift, UPDATE the row + write an
       ``IB_MIRROR_UPDATE`` order_event recording before/after. On no drift,
       no-op. Returns ``{"action": "UPDATED"|"NOOP", "drift": {...}|None}``.
    3. **Existing UUID-authored row** — keep dedupe semantics: this is a
       Xenon-placed order with `modify_sequence` invariants we won't
       silently violate from a TWS-side change. Returns
       ``{"action": "SKIPPED_UUID", "drift": None}``.

    Branch (2) closes the bug where TWS price/qty edits never reached our
    DB: the previous insert-only behavior left snapshot rows frozen at
    their first-seen values.
    """
    submission_id = f"snapshot-{perm_id}"
    client_attempt_id = f"snapshot-{perm_id}"
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.limit_price,
                order_submissions.c.quantity,
                order_submissions.c.state,
            ).where(
                order_submissions.c.perm_id == str(perm_id),
                order_submissions.c.broker == broker,
                order_submissions.c.account_env == account_env,
                order_submissions.c.broker_account == broker_account,
            )
        ).first()

        # Pull existing tif separately so we don't break the positional unpack
        # of older callers if this query expands further.
        existing_tif: str | None = None
        if existing is not None:
            existing_tif_row = conn.execute(
                select(order_submissions.c.tif).where(order_submissions.c.submission_id == existing[0])
            ).first()
            existing_tif = str(existing_tif_row[0]) if existing_tif_row and existing_tif_row[0] is not None else None

        if existing is not None:
            existing_submission_id = existing[0]
            existing_limit_price = float(existing[1]) if existing[1] is not None else None
            existing_quantity = int(existing[2]) if existing[2] is not None else None
            existing_state = str(existing[3]) if existing[3] is not None else ""

            # UUID-authored rows are off-limits: their modify_sequence is
            # the source of truth. TWS-side modifies on those need a
            # separate, conscious policy decision.
            if not existing_submission_id.startswith("snapshot-"):
                return {"action": "SKIPPED_UUID", "drift": None}

            # IB still reports this order as open while we have it in a
            # terminal state — restore it. Operator error, race in the
            # cancel route, or out-of-band TWS action can drive this.
            if existing_state in {"CANCELLED", "FILLED", "REJECTED", "FAILED", "UNKNOWN"}:
                conn.execute(
                    update(order_submissions)
                    .where(order_submissions.c.submission_id == existing_submission_id)
                    .values(
                        state="WORKING",
                        reason_code=None,
                        limit_price=float(limit_price),
                        quantity=int(quantity),
                        tif=tif,
                        updated_at=now,
                    )
                )
                conn.execute(
                    insert(order_events).values(
                        submission_id=existing_submission_id,
                        kind="IB_MIRROR_RESURRECT",
                        detail={
                            "from_state": existing_state,
                            "to_state": "WORKING",
                            "reason": "ib_still_reports_open",
                        },
                        at=now,
                    )
                )
                return {"action": "RESURRECTED", "drift": None}

            # Round to 4dp before comparing — the DB column is numeric(12,4)
            # and incoming `limit_price` is a raw float that can carry round-trip
            # noise (e.g. 1.4500000001). Without rounding, every poll tick would
            # spuriously detect drift and emit a redundant IB_MIRROR_UPDATE event.
            new_price = round(float(limit_price), 4)
            stored_price = round(existing_limit_price, 4) if existing_limit_price is not None else None
            drift: dict = {}
            if stored_price != new_price:
                drift["limit_price"] = {"from": stored_price, "to": new_price}
            if existing_quantity != int(quantity):
                drift["quantity"] = {
                    "from": existing_quantity,
                    "to": int(quantity),
                }
            if existing_tif is not None and existing_tif != tif:
                drift["tif"] = {"from": existing_tif, "to": tif}
            if not drift:
                return {"action": "NOOP", "drift": None}

            conn.execute(
                update(order_submissions)
                .where(order_submissions.c.submission_id == existing_submission_id)
                .values(
                    limit_price=float(limit_price),
                    quantity=int(quantity),
                    tif=tif,
                    updated_at=now,
                )
            )
            conn.execute(
                insert(order_events).values(
                    submission_id=existing_submission_id,
                    kind="IB_MIRROR_UPDATE",
                    detail=drift,
                    at=now,
                )
            )
            return {"action": "UPDATED", "drift": drift}

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
                state="WORKING",
                submitted_at=now,
                updated_at=now,
                modify_sequence=0,
                broker=broker,
                account_env=account_env,
                broker_account=broker_account,
                strike=strike,
                right=right,
                expiry=expiry,
                con_id=con_id,
                tif=tif,
            )
            .on_conflict_do_nothing(index_elements=["submission_id"])
            .returning(order_submissions.c.submission_id)
        )
        if result.first() is None:
            # Race: another writer slipped in between the SELECT and INSERT.
            # Fine to no-op — the next poll tick will see and reconcile.
            return {"action": "NOOP", "drift": None}
        return {"action": "INSERTED", "drift": None}


def apply_modify_by_perm_id(
    perm_id: str,
    sequence: int,
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


def update_fill_commission(
    *,
    exec_id: str,
    commission: Decimal,
    realized_pnl: Decimal | None,
) -> bool:
    """Apply a late-arriving CommissionReport to an existing order_fills row.

    Execution-grain fill fields remain insert-only. IB delivers commission
    and realizedPNL on a separate message, so this writer only patches those
    post-fill report fields and emits an outbox event when anything changed.
    """
    incoming_commission = Decimal(str(commission))
    realized_pnl_text = str(realized_pnl) if realized_pnl is not None else None

    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            select(
                order_fills.c.exec_id,
                order_fills.c.submission_id,
                order_fills.c.combo_attempt_id,
                order_fills.c.commission,
                order_fills.c.metadata,
            )
            .where(order_fills.c.exec_id == exec_id)
            .with_for_update()
        ).first()
        if row is None:
            return False

        fill = row._mapping
        stored_commission = Decimal(fill["commission"] or 0)
        metadata = fill["metadata"] if isinstance(fill["metadata"], dict) else {}
        existing_realized_pnl = metadata.get("realized_pnl")
        existing_realized_pnl_text = str(existing_realized_pnl) if existing_realized_pnl is not None else None

        update_commission = incoming_commission > 0 and stored_commission == 0
        update_realized_pnl = realized_pnl is not None and existing_realized_pnl_text != realized_pnl_text
        if not update_commission and not update_realized_pnl:
            return False

        values = {}
        if update_commission:
            values["commission"] = incoming_commission
        if update_realized_pnl:
            values["metadata"] = func.jsonb_set(
                func.coalesce(order_fills.c.metadata, cast(literal("{}"), JSONB)),
                cast(literal(["realized_pnl"]), ARRAY(Text)),
                cast(literal(json.dumps(realized_pnl_text)), JSONB),
                True,
            )

        conn.execute(update(order_fills).where(order_fills.c.exec_id == exec_id).values(**values))
        emit_outbox_in_txn(
            conn,
            channel=CHANNEL_FILL_COMMISSION_UPDATED,
            source="update_fill_commission",
            payload={
                "exec_id": exec_id,
                "submission_id": fill["submission_id"],
                "combo_attempt_id": fill["combo_attempt_id"],
                "legacy_id": metadata.get("legacy_id"),
                "commission": str(incoming_commission),
                "realized_pnl": realized_pnl_text,
            },
        )
        return True


def record_event(
    submission_id: str,
    kind: str,
    detail: dict,
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
