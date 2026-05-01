"""Queries for the regime_overrides audit table.

Spec: docs/superpowers/specs/2026-04-29-vcg-cri-strategies-rewiring-design.md §4.3.

The composite FK (submission_id, broker, account_env, broker_account) is
DEFERRABLE INITIALLY DEFERRED so `insert_override` can run inside the
same transaction as `orders.reserve_attempt`. The deferred FK is checked
at COMMIT — if the parent submission row never lands, both the override
row and the submission reservation roll back atomically.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import desc, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import regime_overrides


async def insert_override(
    conn: AsyncConnection,
    *,
    user_id: str,
    account_env: str,
    broker: str,
    broker_account: str,
    submission_id: str,
    client_attempt_id: Optional[str],
    route: str,
    vcg_tier: Optional[str],
    cri_tier: Optional[str],
    binding_side: str,
    block_reason: str,
    user_reason: str,
    order_payload: dict[str, Any],
) -> int:
    """Insert a regime_overrides row inside the caller's transaction.

    Returns the new row's id. The composite FK is DEFERRED so the parent
    submission row need not exist yet — but it MUST exist by COMMIT or
    the whole transaction rolls back.
    """
    stmt = (
        insert(regime_overrides)
        .values(
            user_id=user_id,
            account_env=account_env,
            broker=broker,
            broker_account=broker_account,
            submission_id=submission_id,
            client_attempt_id=client_attempt_id,
            route=route,
            vcg_tier=vcg_tier,
            cri_tier=cri_tier,
            binding_side=binding_side,
            block_reason=block_reason,
            user_reason=user_reason,
            order_payload=order_payload,
        )
        .returning(regime_overrides.c.id)
    )
    result = await conn.execute(stmt)
    row_id = result.scalar()
    assert row_id is not None
    return int(row_id)


async def mark_broker_ids(
    conn: AsyncConnection,
    *,
    submission_id: str,
    perm_id: Optional[int],
    ib_order_id: Optional[int],
) -> int:
    """Fill in IB perm_id / ib_order_id post-submit. Returns rows updated.

    Mirrors orders.mark_submitted's two-phase pattern — broker IDs are
    only known after the broker accepts the order, so the audit row is
    written first with NULLs and updated here.
    """
    stmt = (
        update(regime_overrides)
        .where(regime_overrides.c.submission_id == submission_id)
        .values(perm_id=perm_id, ib_order_id=ib_order_id)
    )
    result = await conn.execute(stmt)
    return result.rowcount or 0


async def list_overrides(
    conn: AsyncConnection,
    *,
    account_env: str,
    broker_account: str,
    user_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return overrides for the given scope, newest first.

    `user_id` is optional — when supplied, results are scoped to that
    user (used by GET /regime/overrides for the current Clerk user).
    """
    stmt = (
        select(regime_overrides)
        .where(regime_overrides.c.account_env == account_env)
        .where(regime_overrides.c.broker_account == broker_account)
        .order_by(desc(regime_overrides.c.ts))
        .limit(limit)
    )
    if user_id is not None:
        stmt = stmt.where(regime_overrides.c.user_id == user_id)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def get_override_for_submission(
    conn: AsyncConnection,
    *,
    submission_id: str,
) -> Optional[dict[str, Any]]:
    """Return the override row tied to a submission_id, or None.

    Used by the blotter "Overridden" tag join. Submissions have at most
    one override row by current API design (one entry attempt = one
    optional override).
    """
    stmt = select(regime_overrides).where(regime_overrides.c.submission_id == submission_id).limit(1)
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None
