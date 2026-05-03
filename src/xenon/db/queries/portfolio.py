from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import account_snapshots, nav_history, positions


async def save_positions(
    conn: AsyncConnection,
    rows: list[dict],
    *,
    account: str,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> None:
    await conn.execute(
        delete(positions).where(
            positions.c.account == account,
            positions.c.broker == broker,
            positions.c.account_env == account_env,
            positions.c.broker_account == broker_account,
        )
    )
    for row in rows:
        row.setdefault("broker", broker)
        row.setdefault("account_env", account_env)
        row.setdefault("broker_account", broker_account)
        await conn.execute(insert(positions).values(**row))


async def get_positions(
    conn: AsyncConnection,
    *,
    account: str | None = None,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    stmt = select(positions)
    if account:
        stmt = stmt.where(positions.c.account == account)
    if broker is not None:
        stmt = stmt.where(positions.c.broker == broker)
    if account_env is not None:
        stmt = stmt.where(positions.c.account_env == account_env)
    if broker_account is not None:
        stmt = stmt.where(positions.c.broker_account == broker_account)
    stmt = stmt.order_by(positions.c.ticker)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def save_account_snapshot(
    conn: AsyncConnection,
    *,
    account: str,
    bankroll: Decimal,
    peak_value: Decimal | None = None,
    net_liquidation: Decimal | None = None,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> None:
    await conn.execute(
        insert(account_snapshots).values(
            account=account,
            bankroll=bankroll,
            peak_value=peak_value,
            net_liquidation=net_liquidation,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
    )


async def get_latest_snapshot(
    conn: AsyncConnection,
    *,
    account: str,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> dict | None:
    stmt = select(account_snapshots).where(account_snapshots.c.account == account)
    if broker is not None:
        stmt = stmt.where(account_snapshots.c.broker == broker)
    if account_env is not None:
        stmt = stmt.where(account_snapshots.c.account_env == account_env)
    if broker_account is not None:
        stmt = stmt.where(account_snapshots.c.broker_account == broker_account)
    stmt = stmt.order_by(account_snapshots.c.snapshot_at.desc()).limit(1)
    result = await conn.execute(stmt)
    row = result.first()
    return dict(row._mapping) if row else None


async def get_latest_net_liquidation_for_scope(
    conn: AsyncConnection,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
) -> Decimal | None:
    """Most recent non-null `net_liquidation` for this scope, or None.

    Used as the bankroll input to RegimeGate's throttle-cap math so caps
    are sized against actual account NAV instead of a hardcoded default.
    """
    stmt = (
        select(account_snapshots.c.net_liquidation)
        .where(account_snapshots.c.broker == broker)
        .where(account_snapshots.c.account_env == account_env)
        .where(account_snapshots.c.broker_account == broker_account)
        .where(account_snapshots.c.net_liquidation.is_not(None))
        .order_by(account_snapshots.c.snapshot_at.desc())
        .limit(1)
    )
    result = await conn.execute(stmt)
    row = result.first()
    return row[0] if row and row[0] is not None else None


async def get_latest_portfolio_payload(
    conn: AsyncConnection,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
) -> dict | None:
    """Return the most recent structured portfolio payload for the given scope.

    Reads `account_snapshots.payload` (jsonb), which `_save_portfolio_to_postgres`
    populates with the full UI-shaped dict at sync time. Phase 1 of the
    portfolio postgres read-path migration — see
    docs/plans/2026-04-27-portfolio-postgres-read-path.md.
    """
    stmt = (
        select(account_snapshots.c.payload, account_snapshots.c.snapshot_at)
        .where(account_snapshots.c.broker == broker)
        .where(account_snapshots.c.account_env == account_env)
        .where(account_snapshots.c.broker_account == broker_account)
        .order_by(account_snapshots.c.snapshot_at.desc())
        .limit(1)
    )
    result = await conn.execute(stmt)
    row = result.first()
    if row is None:
        return None
    payload = row.payload or {}
    if not payload:
        return None
    return dict(payload)


async def get_account_snapshots_history(
    conn: AsyncConnection,
    *,
    broker: str,
    account_env: str,
    broker_account: str,
    limit: int = 5,
) -> list[dict]:
    """Return the most recent N account_snapshots rows for the given scope, desc.

    Each row dict carries every column on `xenon.account_snapshots`, including
    `payload` (jsonb) and `snapshot_at`. Used by ib_sync as the entry-date
    fallback chain after the JSON cutoff — see the PG migration plan.
    """
    stmt = (
        select(account_snapshots)
        .where(account_snapshots.c.broker == broker)
        .where(account_snapshots.c.account_env == account_env)
        .where(account_snapshots.c.broker_account == broker_account)
        .order_by(account_snapshots.c.snapshot_at.desc())
        .limit(limit)
    )
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def upsert_nav(
    conn: AsyncConnection,
    day: date,
    *,
    nav: Decimal,
    daily_pnl: Decimal | None = None,
    total: Decimal | None = None,
    cash: Decimal | None = None,
    stock_value: Decimal | None = None,
    options_value: Decimal | None = None,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> None:
    stmt = pg_insert(nav_history).values(
        broker=broker,
        account_env=account_env,
        broker_account=broker_account,
        date=day,
        nav=nav,
        daily_pnl=daily_pnl,
        total=total,
        cash=cash,
        stock_value=stock_value,
        options_value=options_value,
    )
    set_columns: dict[str, object] = {"nav": stmt.excluded.nav, "daily_pnl": stmt.excluded.daily_pnl}
    # Only overwrite breakdown columns when this caller actually has values —
    # ib_sync's daily upsert sends nav only; the IB Flex importer sends the
    # full breakdown. Skipping NULLs preserves whichever source last filled them.
    if total is not None:
        set_columns["total"] = stmt.excluded.total
    if cash is not None:
        set_columns["cash"] = stmt.excluded.cash
    if stock_value is not None:
        set_columns["stock_value"] = stmt.excluded.stock_value
    if options_value is not None:
        set_columns["options_value"] = stmt.excluded.options_value
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            nav_history.c.broker,
            nav_history.c.account_env,
            nav_history.c.broker_account,
            nav_history.c.date,
        ],
        set_=set_columns,
    )
    await conn.execute(stmt)


async def get_nav_history(
    conn: AsyncConnection,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    stmt = select(nav_history)
    if broker is not None:
        stmt = stmt.where(nav_history.c.broker == broker)
    if account_env is not None:
        stmt = stmt.where(nav_history.c.account_env == account_env)
    if broker_account is not None:
        stmt = stmt.where(nav_history.c.broker_account == broker_account)
    stmt = stmt.order_by(nav_history.c.date)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
