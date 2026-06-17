"""Persisted Futu history: typed insert + scope-filtered list helpers.

Source for the backward NAV walk (xenon.api.services.futu_nav_backfill).
Writes go through the M4 sync service; reads happen in M5. CLI subprocesses
build their own AsyncEngine via xenon.db.engine.get_engine().

PK semantics:
  - futu_trades: (broker, account_env, broker_account, futu_deal_id)
  - futu_cash_flow: (broker, account_env, broker_account, futu_flow_id)

`insert_*` UPSERTs (re-pulls are idempotent — Futu's deal/flow IDs are
stable, so the second run replaces fields like fees that may be recomputed
server-side after settlement).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.schema import (
    futu_cash_flow,
    futu_closed_trades,
    futu_daily_statement,
    futu_order_fees,
    futu_orders,
    futu_statement_inbox,
    futu_trades,
    journal_entries,
)
from xenon.execution.account_scope import AccountScope

# Postgres' wire protocol caps a single statement at 32767 bind params.
# Chunk so (rows_per_batch * cols_per_row) stays well under that ceiling.
# futu_trades has 15 columns → 2000 rows = 30k params (safe).
# futu_cash_flow has 10 columns → 3000 rows = 30k params (safe).
# Single constant keeps the math conservative for both shapes.
_INSERT_BATCH_ROWS = 2000


def _scoped(row: dict, scope: AccountScope) -> dict:
    return {
        "broker": scope.broker,
        "account_env": scope.account_env,
        "broker_account": scope.broker_account,
        **row,
    }


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def insert_trades(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    total = 0
    async with engine.begin() as conn:
        for batch in _chunks(rows_list, _INSERT_BATCH_ROWS):
            stmt = pg_insert(futu_trades).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["broker", "account_env", "broker_account", "futu_deal_id"],
                set_={
                    "futu_order_id": stmt.excluded.futu_order_id,
                    "ticker": stmt.excluded.ticker,
                    "futu_code": stmt.excluded.futu_code,
                    "market": stmt.excluded.market,
                    "action": stmt.excluded.action,
                    "quantity": stmt.excluded.quantity,
                    "price": stmt.excluded.price,
                    "fees": stmt.excluded.fees,
                    "filled_at": stmt.excluded.filled_at,
                    "raw": stmt.excluded.raw,
                },
            )
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


async def insert_cashflows(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    total = 0
    async with engine.begin() as conn:
        for batch in _chunks(rows_list, _INSERT_BATCH_ROWS):
            stmt = pg_insert(futu_cash_flow).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["broker", "account_env", "broker_account", "futu_flow_id"],
                set_={
                    "cashflow_type": stmt.excluded.cashflow_type,
                    "amount": stmt.excluded.amount,
                    "currency": stmt.excluded.currency,
                    "occurred_at": stmt.excluded.occurred_at,
                    "raw": stmt.excluded.raw,
                },
            )
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


async def list_trades(
    engine: AsyncEngine,
    scope: AccountScope,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    where = (
        (futu_trades.c.broker == scope.broker)
        & (futu_trades.c.account_env == scope.account_env)
        & (futu_trades.c.broker_account == scope.broker_account)
    )
    if since is not None:
        where = where & (futu_trades.c.filled_at >= since)
    if until is not None:
        where = where & (futu_trades.c.filled_at <= until)
    stmt = sa.select(futu_trades).where(where).order_by(futu_trades.c.filled_at.asc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


async def list_cashflows(
    engine: AsyncEngine,
    scope: AccountScope,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    where = (
        (futu_cash_flow.c.broker == scope.broker)
        & (futu_cash_flow.c.account_env == scope.account_env)
        & (futu_cash_flow.c.broker_account == scope.broker_account)
    )
    if since is not None:
        where = where & (futu_cash_flow.c.occurred_at >= since)
    if until is not None:
        where = where & (futu_cash_flow.c.occurred_at <= until)
    stmt = sa.select(futu_cash_flow).where(where).order_by(futu_cash_flow.c.occurred_at.asc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


# --- Futu orders / fees / closed-trades (read-only order querying) ---------

# futu_orders has 20 columns → 1500 rows = 30k params (safe under the 32767 ceiling).
_ORDERS_BATCH_ROWS = 1500


def _upsert_set(table, *, exclude: set[str]):
    """All non-PK, non-`ingested_at` columns mapped to their EXCLUDED value."""
    pk = {c.name for c in table.primary_key.columns}
    return {c.name: c for c in table.columns if c.name not in pk and c.name not in exclude}


async def insert_orders(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    total = 0
    async with engine.begin() as conn:
        for batch in _chunks(rows_list, _ORDERS_BATCH_ROWS):
            stmt = pg_insert(futu_orders).values(batch)
            update_cols = {
                name: getattr(stmt.excluded, name) for name in _upsert_set(futu_orders, exclude={"ingested_at"})
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["broker", "account_env", "broker_account", "futu_order_id"],
                set_=update_cols,
            )
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


async def list_orders(
    engine: AsyncEngine,
    scope: AccountScope,
    *,
    statuses: Iterable[str] | None = None,
) -> list[dict]:
    where = (
        (futu_orders.c.broker == scope.broker)
        & (futu_orders.c.account_env == scope.account_env)
        & (futu_orders.c.broker_account == scope.broker_account)
    )
    if statuses is not None:
        where = where & (futu_orders.c.status.in_(list(statuses)))
    stmt = sa.select(futu_orders).where(where).order_by(futu_orders.c.updated_at.desc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


async def insert_order_fees(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    total = 0
    async with engine.begin() as conn:
        for batch in _chunks(rows_list, _INSERT_BATCH_ROWS):
            stmt = pg_insert(futu_order_fees).values(batch)
            update_cols = {
                name: getattr(stmt.excluded, name) for name in _upsert_set(futu_order_fees, exclude={"ingested_at"})
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["broker", "account_env", "broker_account", "futu_order_id"],
                set_=update_cols,
            )
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


async def insert_closed_trades(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    total = 0
    async with engine.begin() as conn:
        for batch in _chunks(rows_list, _ORDERS_BATCH_ROWS):
            stmt = pg_insert(futu_closed_trades).values(batch)
            update_cols = {
                name: getattr(stmt.excluded, name) for name in _upsert_set(futu_closed_trades, exclude={"ingested_at"})
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["broker", "account_env", "broker_account", "futu_close_id"],
                set_=update_cols,
            )
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


async def list_closed_trades(
    engine: AsyncEngine,
    scope: AccountScope,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    where = (
        (futu_closed_trades.c.broker == scope.broker)
        & (futu_closed_trades.c.account_env == scope.account_env)
        & (futu_closed_trades.c.broker_account == scope.broker_account)
    )
    if since is not None:
        where = where & (futu_closed_trades.c.closed_at >= since)
    if until is not None:
        where = where & (futu_closed_trades.c.closed_at <= until)
    stmt = sa.select(futu_closed_trades).where(where).order_by(futu_closed_trades.c.closed_at.desc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


async def insert_futu_journal_entries(engine: AsyncEngine, scope: AccountScope, grouped_rows: list[dict]) -> int:
    """Sync the scope's FUTU_AUTO_IMPORT journal rows to the grouped structure set.

    Takes structure-level groups (from `group_closed_trades`): one journal row per
    closing order — ticker = underlying, structure = the classified name — so the
    journal matches the HISTORICAL blotter. Idempotent + self-healing:

    - UPSERTs each group keyed by `futu_close_id` (the close-order group key),
      refreshing ticker/metadata so structure-name + aggregate changes propagate;
    - DELETEs scope FUTU_AUTO_IMPORT rows NOT in the current set, which purges the
      legacy per-lot entries written before grouping (and structures that no
      longer exist).

    Skips the purge when `grouped_rows` is empty so a transient empty pull can't
    wipe the journal. Shares `build_futu_auto_import_values` with the sync upsert.
    """
    # Lazy import avoids any import-order coupling with the journal query module.
    from xenon.db.queries.journal import FUTU_AUTO_IMPORT_CONFLICT, build_futu_auto_import_values

    if not grouped_rows:
        return 0
    values = [build_futu_auto_import_values(scope, r) for r in grouped_rows]
    keep_keys = [v["futu_close_id"] for v in values]
    total = 0
    async with engine.begin() as conn:
        # Purge legacy/stale FUTU_AUTO_IMPORT rows not in the current grouped set.
        await conn.execute(
            sa.delete(journal_entries).where(
                journal_entries.c.broker == scope.broker,
                journal_entries.c.account_env == scope.account_env,
                journal_entries.c.broker_account == scope.broker_account,
                journal_entries.c.decision == "FUTU_AUTO_IMPORT",
                journal_entries.c.futu_close_id.isnot(None),
                journal_entries.c.futu_close_id.notin_(keep_keys),
            )
        )
        for batch in _chunks(values, _ORDERS_BATCH_ROWS):
            stmt = pg_insert(journal_entries).values(batch)
            stmt = stmt.on_conflict_do_update(
                **FUTU_AUTO_IMPORT_CONFLICT,
                set_={
                    "ticker": stmt.excluded.ticker,
                    "metadata": stmt.excluded.metadata,
                    "authored_at": stmt.excluded.authored_at,
                },
            )
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


async def insert_daily_statement(engine: AsyncEngine, scope: AccountScope, row: dict) -> int:
    """UPSERT one daily-statement row keyed by (scope, statement_date)."""
    scoped = _scoped(row, scope)
    async with engine.begin() as conn:
        stmt = pg_insert(futu_daily_statement).values(**scoped)
        # Re-pulls replace mutable fields. PK columns + ingested_at stay put.
        update_cols = {
            c.name: getattr(stmt.excluded, c.name)
            for c in futu_daily_statement.columns
            if c.name
            not in {
                "broker",
                "account_env",
                "broker_account",
                "statement_date",
                "ingested_at",
            }
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "broker",
                "account_env",
                "broker_account",
                "statement_date",
            ],
            set_=update_cols,
        )
        result = await conn.execute(stmt)
    return result.rowcount or 0


async def insert_statement_inbox(engine: AsyncEngine, scope: AccountScope, row: dict) -> int:
    """UPSERT one raw-PDF row keyed by (scope, source_uid).

    Used when the typed parser raises StatementDecryptError or
    StatementParseError — the raw bytes are preserved so we can re-parse
    offline once the parser learns the missing layout.
    """
    scoped = _scoped(row, scope)
    async with engine.begin() as conn:
        stmt = pg_insert(futu_statement_inbox).values(**scoped)
        update_cols = {
            c.name: getattr(stmt.excluded, c.name)
            for c in futu_statement_inbox.columns
            if c.name
            not in {
                "broker",
                "account_env",
                "broker_account",
                "source_uid",
                "ingested_at",
            }
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "broker",
                "account_env",
                "broker_account",
                "source_uid",
            ],
            set_=update_cols,
        )
        result = await conn.execute(stmt)
    return result.rowcount or 0


async def list_statement_inbox_uids(engine: AsyncEngine, scope: AccountScope) -> set[str]:
    """Return the set of IMAP UIDs already persisted to the inbox for this scope."""
    where = (
        (futu_statement_inbox.c.broker == scope.broker)
        & (futu_statement_inbox.c.account_env == scope.account_env)
        & (futu_statement_inbox.c.broker_account == scope.broker_account)
    )
    stmt = sa.select(futu_statement_inbox.c.source_uid).where(where)
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
        return {row[0] for row in result.fetchall()}


async def list_daily_statements(
    engine: AsyncEngine,
    scope: AccountScope,
    since: datetime | None = None,
    until: datetime | None = None,
    *,
    include_raw_pdf: bool = False,
) -> list[dict]:
    """Return statements for the scope in ascending statement_date order.

    `raw_pdf` is excluded by default since it's bytes-heavy; pass
    include_raw_pdf=True for re-parsing or re-archival flows.
    """
    where = (
        (futu_daily_statement.c.broker == scope.broker)
        & (futu_daily_statement.c.account_env == scope.account_env)
        & (futu_daily_statement.c.broker_account == scope.broker_account)
    )
    if since is not None:
        if isinstance(since, datetime):
            since = since.date()
        where = where & (futu_daily_statement.c.statement_date >= since)
    if until is not None:
        if isinstance(until, datetime):
            until = until.date()
        where = where & (futu_daily_statement.c.statement_date <= until)
    cols = [c for c in futu_daily_statement.columns if c.name != "raw_pdf" or include_raw_pdf]
    stmt = sa.select(*cols).where(where).order_by(futu_daily_statement.c.statement_date.asc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


__all__: Sequence[str] = (
    "insert_trades",
    "insert_cashflows",
    "insert_daily_statement",
    "list_trades",
    "list_cashflows",
    "list_daily_statements",
)
