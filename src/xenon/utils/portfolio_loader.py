"""Single read seam for the IB portfolio payload from Postgres.

Phase 2 of the postgres-read-path migration — see
docs/plans/2026-04-27-portfolio-postgres-read-path-phase2.md.

Sync-only on purpose: the FastAPI hot path already has
`xenon.db.queries.portfolio.get_latest_portfolio_payload` (async). This
module exists for sync subprocesses (ib_sync, ib_reconcile,
naked_short_audit), CLI scripts (scanners, ratings, reports), and
non-async services (uw_analyze_candidates).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal as _Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as _pg_insert

from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots, nav_history, order_fills, trades
from xenon.execution.account_scope import AccountScope


@dataclass(frozen=True)
class EntryDateLookups:
    """PG-derived entry-date fallback lookups for ib_sync.convert_to_portfolio_format.

    Replaces the JSON triple-fallback chain (data/blotter.json + trade_log.json
    + portfolio.json) with one composite query result.

    - per_contract_dates: keyed by ``"{ticker}|{expiry}|{right}|{strike}"`` from
      ``order_fills.metadata``; earliest filled_at per option contract.
    - per_ticker_dates: keyed by ticker; earliest filled_at across all fills
      for the scope. Used as the broader fallback when contract-level keys
      don't match (stock positions, structures we couldn't classify).
    - trade_log_dates: keyed by ``"{ticker}|{structure}"`` and ``"{ticker}"``
      from ``trades.opened_at``. Mirrors the old trade_log.json shape.
    - prev_portfolio_dates: keyed by ``"{ticker}|{structure}|{expiry}"`` from
      the most recent NOT-today snapshot in account_snapshots.payload.
    """

    per_contract_dates: dict[str, str]
    per_ticker_dates: dict[str, str]
    trade_log_dates: dict[str, str]
    prev_portfolio_dates: dict[str, str]


def load_portfolio_payload_sync(*, scope: AccountScope | None = None) -> dict[str, Any] | None:
    """Return the latest IB portfolio payload from Postgres, or None.

    When `scope` is provided, only rows matching that broker/env/account
    are considered. This is mandatory for safety-critical readers
    (preflight, naked-short audit, reconcile) that must not blend
    paper/live data.

    When `scope` is None, returns the absolute latest snapshot regardless
    of scope. Use only for scope-naive discovery (scanner ticker sets,
    analyst-ratings target list, UW candidate underlyings) where mixing
    accounts is acceptable.
    """
    stmt = select(account_snapshots.c.payload).order_by(account_snapshots.c.snapshot_at.desc()).limit(1)
    if scope is not None:
        stmt = (
            select(account_snapshots.c.payload)
            .where(account_snapshots.c.broker == scope.broker)
            .where(account_snapshots.c.account_env == scope.account_env)
            .where(account_snapshots.c.broker_account == scope.broker_account)
            .order_by(account_snapshots.c.snapshot_at.desc())
            .limit(1)
        )
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(stmt).first()
    if row is None:
        return None
    payload = row.payload or {}
    return dict(payload) if payload else None


def load_account_snapshots_history_sync(*, scope: AccountScope, limit: int = 5) -> list[dict[str, Any]]:
    """Return the most recent N account_snapshots rows for the given scope, desc.

    Sync mirror of `xenon.db.queries.portfolio.get_account_snapshots_history`.
    Used by sync subprocesses (ib_sync entry-date fallback) after the JSON
    cutoff — see the PG migration plan.
    """
    stmt = (
        select(account_snapshots)
        .where(account_snapshots.c.broker == scope.broker)
        .where(account_snapshots.c.account_env == scope.account_env)
        .where(account_snapshots.c.broker_account == scope.broker_account)
        .order_by(account_snapshots.c.snapshot_at.desc())
        .limit(limit)
    )
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(stmt)
        return [dict(row._mapping) for row in result]


def load_nav_history_sync(*, scope: AccountScope) -> list[dict[str, Any]]:
    """Return the full NAV history for a scope, ordered by date asc.

    Each row carries `date`, `nav`, `daily_pnl`, plus the IB Flex breakdown
    columns when present (`total`, `cash`, `stock_value`, `options_value`).
    Replaces both `data/nav_history.jsonl` and `data/nav_history_ib.json`.
    """
    stmt = (
        select(nav_history)
        .where(nav_history.c.broker == scope.broker)
        .where(nav_history.c.account_env == scope.account_env)
        .where(nav_history.c.broker_account == scope.broker_account)
        .order_by(nav_history.c.date)
    )
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(stmt)
        return [dict(row._mapping) for row in result]


def _build_upsert_stmt(
    scope: AccountScope,
    day: _date,
    nav,
    daily_pnl,
    total,
    cash,
    stock_value,
    options_value,
    source: str | None,
):
    """Compose the pg_insert(nav_history)…on_conflict_do_update statement.

    Pass-3 A1: ``index_elements`` MUST match the post-migration 5-col PK
    ``(broker, account_env, broker_account, date, source)``. The 4-col form
    references a constraint that no longer exists post-migration and every
    UPSERT would raise `there is no unique or exclusion constraint matching
    the ON CONFLICT specification`.
    """
    values: dict[str, object] = {
        "broker": scope.broker,
        "account_env": scope.account_env,
        "broker_account": scope.broker_account,
        "date": day,
        "nav": nav,
        "daily_pnl": daily_pnl,
        "total": total,
        "cash": cash,
        "stock_value": stock_value,
        "options_value": options_value,
    }
    if source is not None:
        values["source"] = source
    stmt = _pg_insert(nav_history).values(**values)
    set_columns: dict[str, object] = {"nav": stmt.excluded.nav}
    for col_name, col_val in (
        ("daily_pnl", daily_pnl),
        ("total", total),
        ("cash", cash),
        ("stock_value", stock_value),
        ("options_value", options_value),
    ):
        if col_val is not None:
            set_columns[col_name] = getattr(stmt.excluded, col_name)
    return stmt.on_conflict_do_update(
        index_elements=[
            nav_history.c.broker,
            nav_history.c.account_env,
            nav_history.c.broker_account,
            nav_history.c.date,
            nav_history.c.source,
        ],
        set_=set_columns,
    )


def upsert_nav_sync(
    *,
    scope: AccountScope,
    day: _date,
    nav: _Decimal | float | int,
    daily_pnl: _Decimal | float | int | None = None,
    total: _Decimal | float | int | None = None,
    cash: _Decimal | float | int | None = None,
    stock_value: _Decimal | float | int | None = None,
    options_value: _Decimal | float | int | None = None,
    source: str | None = None,
    enforce_account_env_guard: bool = True,
) -> None:
    """Unified sync nav_history writer.

    NULL-safe on every nullable column (None → preserve existing value on
    conflict). ``source`` distinguishes post-close from intraday rows; after
    migration 2026_06_03_nav_history_source_in_pk both can coexist for the
    same (broker, account_env, broker_account, date).

    ``enforce_account_env_guard`` (Pass-2 T3 — default ON): when True,
    refuses to write a row whose (broker, broker_account, date) already
    has a different ``account_env``. The defense has two tiers:

      1. SELECT-before-INSERT for the in-process case.
      2. IntegrityError catch + rollback + re-query winner for the
         inter-process race (two writers both pass the SELECT, only one
         wins the unique-index INSERT). Pass-2 T5: SELECT alone is NOT
         race-safe; the catch is the actual mechanism.

    Legacy unscoped callers can opt out via ``_upsert_nav_sync_unguarded``.
    """
    # Lazy import to avoid circular dependency with futu_nav_persistence.
    from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict

    engine = get_sync_engine()

    if enforce_account_env_guard:
        with engine.begin() as conn:
            existing = conn.execute(
                sa.select(nav_history.c.account_env).where(
                    (nav_history.c.broker == scope.broker)
                    & (nav_history.c.broker_account == scope.broker_account)
                    & (nav_history.c.date == day)
                )
            ).first()
        if existing is not None and existing.account_env != scope.account_env:
            raise NavAccountEnvConflict(scope, existing.account_env, day)

    stmt = _build_upsert_stmt(scope, day, nav, daily_pnl, total, cash, stock_value, options_value, source)
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except sa.exc.IntegrityError:
        if not enforce_account_env_guard:
            raise
        # Inter-process race: re-query and surface NavAccountEnvConflict if
        # the winner had a different account_env. Otherwise re-raise the
        # original IntegrityError (some other constraint fired).
        with engine.begin() as conn2:
            winner = conn2.execute(
                sa.select(nav_history.c.account_env).where(
                    (nav_history.c.broker == scope.broker)
                    & (nav_history.c.broker_account == scope.broker_account)
                    & (nav_history.c.date == day)
                )
            ).first()
        if winner is not None and winner.account_env != scope.account_env:
            raise NavAccountEnvConflict(scope, winner.account_env, day)
        raise


def _upsert_nav_sync_unguarded(**kwargs) -> None:
    """ESCAPE HATCH — bypasses the cross-env guard.

    Only for legacy unscoped backfill code that proves no concurrent writers
    exist. New code MUST use ``upsert_nav_sync`` directly.
    """
    upsert_nav_sync(enforce_account_env_guard=False, **kwargs)


async def upsert_nav_async(
    engine,  # AsyncEngine
    *,
    scope: AccountScope,
    day: _date,
    nav: _Decimal | float | int,
    daily_pnl: _Decimal | float | int | None = None,
    total: _Decimal | float | int | None = None,
    cash: _Decimal | float | int | None = None,
    stock_value: _Decimal | float | int | None = None,
    options_value: _Decimal | float | int | None = None,
    source: str | None = None,
    enforce_account_env_guard: bool = True,
) -> None:
    """Async surface — used by FastAPI callers (persist_futu_nav).

    Same race-safe semantics as ``upsert_nav_sync`` but against an AsyncEngine.
    Pass-2 T6: no event-loop blocking from ``get_sync_engine()`` inside an
    async route.
    """
    from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict

    if enforce_account_env_guard:
        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    sa.select(nav_history.c.account_env).where(
                        (nav_history.c.broker == scope.broker)
                        & (nav_history.c.broker_account == scope.broker_account)
                        & (nav_history.c.date == day)
                    )
                )
            ).first()
        if existing is not None and existing.account_env != scope.account_env:
            raise NavAccountEnvConflict(scope, existing.account_env, day)

    stmt = _build_upsert_stmt(scope, day, nav, daily_pnl, total, cash, stock_value, options_value, source)
    try:
        async with engine.begin() as conn:
            await conn.execute(stmt)
    except sa.exc.IntegrityError:
        if not enforce_account_env_guard:
            raise
        async with engine.begin() as conn2:
            winner = (
                await conn2.execute(
                    sa.select(nav_history.c.account_env).where(
                        (nav_history.c.broker == scope.broker)
                        & (nav_history.c.broker_account == scope.broker_account)
                        & (nav_history.c.date == day)
                    )
                )
            ).first()
        if winner is not None and winner.account_env != scope.account_env:
            raise NavAccountEnvConflict(scope, winner.account_env, day)
        raise


def load_entry_date_lookups_sync(*, scope: AccountScope) -> EntryDateLookups:
    """Build all four entry-date lookup dicts from PG in one engine context.

    Replaces the JSON fallback chain in ib_sync.convert_to_portfolio_format
    (data/blotter.json + trade_log.json + portfolio.json reads). The semantics
    mirror what the JSON code did:

    - per_contract_dates: ``MIN(filled_at)`` per (ticker, expiry, right, strike)
      from order_fills.metadata. Date format ``YYYY-MM-DD`` to match old shape.
    - per_ticker_dates: ``MIN(filled_at)`` per ticker from order_fills.
    - trade_log_dates: latest ``opened_at`` per (ticker, structure) from trades,
      stored under both ``"{ticker}|{structure}"`` and ``"{ticker}"`` keys.
    - prev_portfolio_dates: ``entry_date`` from positions in the most recent
      NOT-today account_snapshots row, keyed by ``"{ticker}|{structure}|{expiry}"``.
    """
    from datetime import datetime as _datetime
    from datetime import timezone as _timezone

    today = _datetime.now(_timezone.utc).strftime("%Y-%m-%d")
    engine = get_sync_engine()
    with engine.begin() as conn:
        # Per-contract earliest fill from order_fills.metadata (option fills only)
        meta = order_fills.c.metadata
        per_contract_stmt = (
            select(
                order_fills.c.ticker,
                meta["expiry"].astext.label("expiry"),
                meta["right"].astext.label("right"),
                meta["strike"].astext.label("strike"),
                func.min(order_fills.c.filled_at).label("first_fill"),
            )
            .where(order_fills.c.broker == scope.broker)
            .where(order_fills.c.account_env == scope.account_env)
            .where(order_fills.c.broker_account == scope.broker_account)
            .where(meta["expiry"].astext.isnot(None))
            .where(meta["right"].astext.isnot(None))
            .where(meta["strike"].astext.isnot(None))
            .group_by(order_fills.c.ticker, "expiry", "right", "strike")
        )
        per_contract: dict[str, str] = {}
        for row in conn.execute(per_contract_stmt):
            try:
                strike_f = float(row.strike)
            except (TypeError, ValueError):
                continue
            key = f"{row.ticker}|{row.expiry}|{row.right}|{strike_f}"
            per_contract[key] = row.first_fill.strftime("%Y-%m-%d")

        # Per-ticker earliest fill (broader fallback)
        per_ticker_stmt = (
            select(order_fills.c.ticker, func.min(order_fills.c.filled_at).label("first_fill"))
            .where(order_fills.c.broker == scope.broker)
            .where(order_fills.c.account_env == scope.account_env)
            .where(order_fills.c.broker_account == scope.broker_account)
            .group_by(order_fills.c.ticker)
        )
        per_ticker: dict[str, str] = {
            row.ticker: row.first_fill.strftime("%Y-%m-%d") for row in conn.execute(per_ticker_stmt)
        }

        # trade_log_dates from trades.opened_at (one per ticker+structure, most recent)
        trade_stmt = (
            select(trades.c.ticker, trades.c.structure, func.max(trades.c.opened_at).label("last_open"))
            .where(trades.c.broker == scope.broker)
            .where(trades.c.account_env == scope.account_env)
            .where(trades.c.broker_account == scope.broker_account)
            .where(trades.c.opened_at.isnot(None))
            .group_by(trades.c.ticker, trades.c.structure)
        )
        trade_log: dict[str, str] = {}
        for row in conn.execute(trade_stmt):
            d = row.last_open.strftime("%Y-%m-%d")
            trade_log[row.ticker] = d
            if row.structure:
                trade_log[f"{row.ticker}|{row.structure}"] = d

        # Previous portfolio dates from the latest account_snapshot whose
        # entry_dates are not all stamped today (avoids inheriting the old bug
        # where every sync set entry_date = today).
        snap_stmt = (
            select(account_snapshots.c.payload, account_snapshots.c.snapshot_at)
            .where(account_snapshots.c.broker == scope.broker)
            .where(account_snapshots.c.account_env == scope.account_env)
            .where(account_snapshots.c.broker_account == scope.broker_account)
            .order_by(account_snapshots.c.snapshot_at.desc())
            .limit(5)
        )
        prev_portfolio: dict[str, str] = {}
        for row in conn.execute(snap_stmt):
            payload = row.payload or {}
            for p in payload.get("positions", []):
                key = f"{p.get('ticker')}|{p.get('structure')}|{p.get('expiry')}"
                ed = p.get("entry_date", "")
                if ed and ed != today and key not in prev_portfolio:
                    prev_portfolio[key] = ed
            if prev_portfolio:
                break

    return EntryDateLookups(
        per_contract_dates=per_contract,
        per_ticker_dates=per_ticker,
        trade_log_dates=trade_log,
        prev_portfolio_dates=prev_portfolio,
    )


def get_portfolio_tickers_sync(*, scope: AccountScope | None = None) -> list[str]:
    """Convenience wrapper: extract unique uppercase tickers from the latest snapshot."""
    payload = load_portfolio_payload_sync(scope=scope)
    if not payload:
        return []
    seen: set[str] = set()
    for pos in payload.get("positions", []):
        ticker = str(pos.get("ticker", "")).upper().strip()
        if ticker:
            seen.add(ticker)
    return sorted(seen)
