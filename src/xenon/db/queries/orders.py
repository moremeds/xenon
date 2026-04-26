from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import order_events, order_submissions


async def reserve_attempt(
    conn: AsyncConnection,
    *,
    submission_id: str,
    user_id: str,
    client_attempt_id: str,
    ticker: str,
    security_type: str,
    action: str,
    quantity: int,
    limit_price: Decimal,
    expiry=None,
    strike=None,
    right=None,
    multiplier: int = 100,
    con_id: int | None = None,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> dict:
    now = datetime.now(timezone.utc)
    values = dict(
        submission_id=submission_id,
        user_id=user_id,
        client_attempt_id=client_attempt_id,
        ticker=ticker,
        security_type=security_type,
        action=action,
        quantity=quantity,
        limit_price=limit_price,
        expiry=expiry,
        strike=strike,
        right=right,
        multiplier=multiplier,
        con_id=con_id,
        state="PENDING",
        submitted_at=now,
        updated_at=now,
        broker=broker,
        account_env=account_env,
        broker_account=broker_account,
    )
    stmt = pg_insert(order_submissions).values(**values)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_order_sub_user_attempt")
    stmt = stmt.returning(order_submissions.c.submission_id)
    result = await conn.execute(stmt)
    inserted = result.first()
    if inserted is not None:
        return await get_by_submission_id(conn, submission_id)
    existing = await lookup_by_attempt(
        conn,
        user_id,
        client_attempt_id,
        broker=broker,
        account_env=account_env,
        broker_account=broker_account,
    )
    return existing


async def get_by_submission_id(conn: AsyncConnection, submission_id: str) -> dict | None:
    stmt = select(order_submissions).where(order_submissions.c.submission_id == submission_id)
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def mark_submitted(
    conn: AsyncConnection,
    *,
    submission_id: str,
    ib_order_id: int,
    perm_id: int,
    placing_client_id: int,
) -> None:
    await conn.execute(
        update(order_submissions)
        .where(order_submissions.c.submission_id == submission_id)
        .values(
            state="WORKING",
            ib_order_id=str(ib_order_id),
            perm_id=str(perm_id),
            placing_client_id=placing_client_id,
            updated_at=datetime.now(timezone.utc),
        )
    )


async def mark_terminal(
    conn: AsyncConnection,
    *,
    submission_id: str,
    state: str,
    reason_code: str | None = None,
    filled_qty: int = 0,
    avg_fill_price: Decimal | None = None,
) -> None:
    await conn.execute(
        update(order_submissions)
        .where(order_submissions.c.submission_id == submission_id)
        .values(
            state=state,
            reason_code=reason_code,
            filled_qty=filled_qty,
            avg_fill_price=avg_fill_price,
            updated_at=datetime.now(timezone.utc),
        )
    )


async def apply_modify(conn: AsyncConnection, *, submission_id: str, modify_sequence: int) -> dict:
    result = await conn.execute(
        update(order_submissions)
        .where(
            order_submissions.c.submission_id == submission_id,
            order_submissions.c.modify_sequence < modify_sequence,
        )
        .values(
            modify_sequence=modify_sequence,
            updated_at=datetime.now(timezone.utc),
        )
        .returning(order_submissions.c.modify_sequence)
    )
    row = result.first()
    if row:
        return {"applied": True, "current_sequence": row[0]}
    existing = await conn.execute(
        select(order_submissions.c.modify_sequence).where(order_submissions.c.submission_id == submission_id)
    )
    ex_row = existing.first()
    if ex_row:
        return {"applied": False, "current_sequence": ex_row[0]}
    return {"applied": False, "current_sequence": -1}


async def record_event(
    conn: AsyncConnection,
    *,
    submission_id: str,
    kind: str,
    detail: dict | None = None,
) -> None:
    await conn.execute(insert(order_events).values(submission_id=submission_id, kind=kind, detail=detail))


async def get_events(conn: AsyncConnection, *, submission_id: str) -> list[dict]:
    stmt = select(order_events).where(order_events.c.submission_id == submission_id).order_by(order_events.c.at)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def lookup_by_perm_id(conn: AsyncConnection, perm_id: int | str) -> str | None:
    stmt = select(order_submissions.c.submission_id).where(order_submissions.c.perm_id == str(perm_id))
    row = (await conn.execute(stmt)).first()
    return row[0] if row else None


async def lookup_by_ib_order_id(conn: AsyncConnection, ib_order_id: int | str) -> str | None:
    stmt = select(order_submissions.c.submission_id).where(order_submissions.c.ib_order_id == str(ib_order_id))
    row = (await conn.execute(stmt)).first()
    return row[0] if row else None


async def lookup_by_attempt(
    conn: AsyncConnection,
    user_id: str,
    client_attempt_id: str,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> dict | None:
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
    stmt = select(order_submissions).where(*conditions)
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def working_orders_for(
    conn: AsyncConnection,
    *,
    user_id: str,
    ticker: str,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    conditions = [
        order_submissions.c.user_id == user_id,
        order_submissions.c.ticker == ticker,
        order_submissions.c.state.in_(["PENDING", "WORKING", "PARTIALLY_FILLED"]),
    ]
    if broker is not None:
        conditions.append(order_submissions.c.broker == broker)
    if account_env is not None:
        conditions.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(order_submissions.c.broker_account == broker_account)
    stmt = select(order_submissions).where(*conditions)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
