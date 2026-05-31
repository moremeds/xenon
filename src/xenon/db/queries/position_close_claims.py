"""Position-level close-claim CRUD. Spec §5.6."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from xenon.db.schema import position_close_claims
from xenon.execution.brackets.close_claim import derive_order_ref


def try_claim(
    engine: Engine,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
    position_key: str,
    claimed_by_protection_id: int,
    claim_kind: str,
) -> int | None:
    """Atomically claim close authority for a position, or return None."""
    temporary_ref = (
        f"__pending__:{broker}:{account_env}:{broker_account}:"
        f"{position_key}:{claimed_by_protection_id}:{claim_kind}"
    )
    with engine.begin() as conn:
        stmt = (
            pg_insert(position_close_claims)
            .values(
                broker=broker,
                account_env=account_env,
                broker_account=broker_account,
                position_key=position_key,
                claimed_by_protection_id=claimed_by_protection_id,
                claim_kind=claim_kind,
                status="PENDING",
                order_ref=temporary_ref,
            )
            .on_conflict_do_nothing()
            .returning(position_close_claims.c.claim_id)
        )
        row = conn.execute(stmt).first()
        if row is None:
            return None

        claim_id = int(row[0])
        conn.execute(
            update(position_close_claims)
            .where(position_close_claims.c.claim_id == claim_id)
            .values(order_ref=derive_order_ref(claim_id=claim_id))
        )
        return claim_id


def mark_submitted(
    engine: Engine,
    *,
    claim_id: int,
    broker_perm_id: int | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(position_close_claims)
            .where(position_close_claims.c.claim_id == claim_id)
            .values(
                status="SUBMITTED",
                broker_perm_id=broker_perm_id,
                attempts=position_close_claims.c.attempts + 1,
                submitted_at=datetime.now(timezone.utc),
            )
        )


def mark_terminal(
    engine: Engine,
    *,
    claim_id: int,
    status: str,
    last_error: str | None = None,
) -> None:
    if status not in ("FILLED", "FAILED", "ABANDONED"):
        raise ValueError(f"invalid terminal claim status: {status!r}")
    with engine.begin() as conn:
        conn.execute(
            update(position_close_claims)
            .where(position_close_claims.c.claim_id == claim_id)
            .values(
                status=status,
                last_error=last_error,
                terminal_at=datetime.now(timezone.utc),
            )
        )


def find_by_order_ref(engine: Engine, *, order_ref: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(position_close_claims).where(position_close_claims.c.order_ref == order_ref)
        ).first()
        return dict(row._mapping) if row else None


def find_inflight_for_position(
    engine: Engine,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
    position_key: str,
) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(position_close_claims).where(
                position_close_claims.c.broker == broker,
                position_close_claims.c.account_env == account_env,
                position_close_claims.c.broker_account == broker_account,
                position_close_claims.c.position_key == position_key,
                position_close_claims.c.status.in_(("PENDING", "SUBMITTED")),
            )
        ).first()
        return dict(row._mapping) if row else None


def increment_attempts(engine: Engine, *, claim_id: int, last_error: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(position_close_claims)
            .where(position_close_claims.c.claim_id == claim_id)
            .values(
                attempts=position_close_claims.c.attempts + 1,
                last_error=last_error,
            )
        )
