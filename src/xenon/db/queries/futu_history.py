"""Typed CRUD helpers for the Futu-history tables.

Every SQL string against futu_trades / futu_cash_flow / corporate_actions /
futu_reconciliation_log / nav_history (Futu rows) / fx_daily MUST live here.
Callers (sync service + FastAPI routes) import named functions.

All trade-date partitioning uses America/New_York TZ:
    (filled_at AT TIME ZONE 'America/New_York')::date

`nav_history.source` was renamed to `nav_type` by migration 2026_06_01_0001
(see Task 1 commit). The public API of write_nav_row / fetch_nav_history
exposes the column as `nav_type` to match the schema.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Iterator

from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope


def _scope_filter() -> str:
    return "broker = :broker AND account_env = :env AND broker_account = :acc"


def _scope_params(scope: AccountScope) -> dict:
    return {
        "broker": scope.broker,
        "env": scope.account_env,
        "acc": scope.broker_account,
    }


# ---- fills ---------------------------------------------------------------

_INSERT_FILL = text(
    """
    INSERT INTO xenon.futu_trades (
        futu_deal_id, futu_order_id, ticker, futu_code, market, action,
        quantity, price, fees, filled_at, raw,
        broker, account_env, broker_account)
    VALUES (:futu_deal_id, :futu_order_id, :ticker, :futu_code, :market,
            :action, :quantity, :price, :fees, :filled_at,
            CAST(:raw AS jsonb), :broker, :env, :acc)
    ON CONFLICT (broker, account_env, broker_account, futu_deal_id)
    DO NOTHING
    """
)


def insert_fills(scope: AccountScope, rows: Iterable[dict]) -> int:
    inserted = 0
    with get_sync_engine().begin() as conn:
        for r in rows:
            res = conn.execute(
                _INSERT_FILL,
                {
                    **_scope_params(scope),
                    **{
                        k: r[k]
                        for k in (
                            "futu_deal_id",
                            "futu_order_id",
                            "ticker",
                            "futu_code",
                            "market",
                            "action",
                            "quantity",
                            "price",
                            "fees",
                            "filled_at",
                        )
                    },
                    "raw": json.dumps(r.get("raw", {}), default=str),
                },
            )
            inserted += res.rowcount
    return inserted


_UPDATE_REALIZED = text(
    """
    UPDATE xenon.futu_trades
       SET realized_pnl = :pnl
     WHERE broker=:broker AND account_env=:env AND broker_account=:acc
       AND futu_deal_id=:deal_id
    """
)


def update_realized_pnl(scope: AccountScope, deal_id: str, pnl: Decimal | None) -> None:
    with get_sync_engine().begin() as conn:
        conn.execute(
            _UPDATE_REALIZED,
            {**_scope_params(scope), "deal_id": deal_id, "pnl": pnl},
        )


def null_realized_for_ticker(scope: AccountScope, ticker: str) -> int:
    """Set realized_pnl back to NULL for every SELL of this ticker.

    Used by the late-arrival rematch path in sync step 4.
    """
    with get_sync_engine().begin() as conn:
        res = conn.execute(
            text(
                f"UPDATE xenon.futu_trades SET realized_pnl = NULL "
                f"WHERE {_scope_filter()} AND ticker=:ticker AND action='SELL'"
            ),
            {**_scope_params(scope), "ticker": ticker},
        )
        return res.rowcount


_FETCH_FILLS = text(
    f"""
    SELECT futu_deal_id, futu_order_id, ticker, futu_code, market, action,
           quantity, price, fees, realized_pnl, filled_at, raw
      FROM xenon.futu_trades
     WHERE {_scope_filter()}
       AND (filled_at AT TIME ZONE 'America/New_York')::date
           BETWEEN :start AND :end
     ORDER BY filled_at, futu_deal_id
    """
)


def fetch_fills(scope: AccountScope, start: date, end: date) -> Iterator[dict]:
    with get_sync_engine().connect() as conn:
        for row in conn.execute(
            _FETCH_FILLS,
            {**_scope_params(scope), "start": start, "end": end},
        ):
            yield dict(row._mapping)


def cursor_for_fills(scope: AccountScope) -> date | None:
    with get_sync_engine().connect() as conn:
        return conn.execute(
            text(
                f"SELECT MAX((filled_at AT TIME ZONE 'America/New_York')::date) "
                f"FROM xenon.futu_trades WHERE {_scope_filter()}"
            ),
            _scope_params(scope),
        ).scalar()


def newest_realized_pnl_at(scope: AccountScope, ticker: str) -> datetime | None:
    with get_sync_engine().connect() as conn:
        return conn.execute(
            text(
                f"SELECT MAX(filled_at) FROM xenon.futu_trades "
                f"WHERE {_scope_filter()} AND ticker=:ticker "
                f"  AND realized_pnl IS NOT NULL"
            ),
            {**_scope_params(scope), "ticker": ticker},
        ).scalar()


# ---- cash flow -----------------------------------------------------------

_INSERT_CASH_FLOW = text(
    """
    INSERT INTO xenon.futu_cash_flow (
        futu_flow_id, flow_type, futu_type_raw, amount, amount_original,
        original_currency, fx_rate_used, ticker, description, flow_at, raw,
        broker, account_env, broker_account)
    VALUES (:futu_flow_id, :flow_type, :futu_type_raw, :amount,
            :amount_original, :original_currency, :fx_rate_used, :ticker,
            :description, :flow_at, CAST(:raw AS jsonb),
            :broker, :env, :acc)
    ON CONFLICT (broker, account_env, broker_account, futu_flow_id)
    DO NOTHING
    """
)


def insert_cash_flows(scope: AccountScope, rows: Iterable[dict]) -> int:
    inserted = 0
    with get_sync_engine().begin() as conn:
        for r in rows:
            res = conn.execute(
                _INSERT_CASH_FLOW,
                {
                    **_scope_params(scope),
                    **{
                        k: r.get(k)
                        for k in (
                            "futu_flow_id",
                            "flow_type",
                            "futu_type_raw",
                            "amount",
                            "amount_original",
                            "original_currency",
                            "fx_rate_used",
                            "ticker",
                            "description",
                            "flow_at",
                        )
                    },
                    "raw": json.dumps(r.get("raw", {}), default=str),
                },
            )
            inserted += res.rowcount
    return inserted


def fetch_cash_flows(scope: AccountScope, start: date, end: date) -> Iterator[dict]:
    with get_sync_engine().connect() as conn:
        for row in conn.execute(
            text(
                f"""
                SELECT futu_flow_id, flow_type, futu_type_raw, amount,
                       amount_original, original_currency, fx_rate_used,
                       ticker, description, flow_at, raw
                  FROM xenon.futu_cash_flow
                 WHERE {_scope_filter()}
                   AND (flow_at AT TIME ZONE 'America/New_York')::date
                       BETWEEN :start AND :end
                 ORDER BY flow_at, futu_flow_id
                """
            ),
            {**_scope_params(scope), "start": start, "end": end},
        ):
            yield dict(row._mapping)


def cursor_for_cash_flows(scope: AccountScope) -> date | None:
    with get_sync_engine().connect() as conn:
        return conn.execute(
            text(
                f"SELECT MAX((flow_at AT TIME ZONE 'America/New_York')::date) "
                f"FROM xenon.futu_cash_flow WHERE {_scope_filter()}"
            ),
            _scope_params(scope),
        ).scalar()


# ---- corporate actions ---------------------------------------------------

_INSERT_CORP = text(
    """
    INSERT INTO xenon.corporate_actions (
        ticker, action_type, effective_date, ratio_numerator,
        ratio_denominator, new_ticker, source, notes)
    VALUES (:ticker, :action_type, :effective_date, :ratio_numerator,
            :ratio_denominator, :new_ticker, 'FUTU_GET_REHAB',
            CAST(:notes AS jsonb))
    ON CONFLICT (ticker, action_type, effective_date) DO NOTHING
    """
)


def insert_corporate_actions(rows: Iterable[dict]) -> int:
    inserted = 0
    with get_sync_engine().begin() as conn:
        for r in rows:
            res = conn.execute(
                _INSERT_CORP,
                {
                    "ticker": r["ticker"],
                    "action_type": r["action_type"],
                    "effective_date": r["effective_date"],
                    "ratio_numerator": r.get("ratio_numerator"),
                    "ratio_denominator": r.get("ratio_denominator"),
                    "new_ticker": r.get("new_ticker"),
                    "notes": json.dumps(r.get("notes", {}), default=str),
                },
            )
            inserted += res.rowcount
    return inserted


def cursor_for_corp_actions(scope: AccountScope) -> date | None:
    """Max effective_date across tickers ever held by this scope."""
    with get_sync_engine().connect() as conn:
        return conn.execute(
            text(
                f"""
                SELECT MAX(ca.effective_date) FROM xenon.corporate_actions ca
                 WHERE ca.ticker IN (
                   SELECT DISTINCT ticker FROM xenon.futu_trades
                    WHERE {_scope_filter()})
                """
            ),
            _scope_params(scope),
        ).scalar()


def fetch_corp_actions_for_tickers(
    tickers: list[str],
) -> Iterator[dict]:
    if not tickers:
        return
    with get_sync_engine().connect() as conn:
        for row in conn.execute(
            text(
                """
                SELECT ticker, action_type, effective_date,
                       ratio_numerator, ratio_denominator, new_ticker
                  FROM xenon.corporate_actions
                 WHERE ticker = ANY(:tickers)
                 ORDER BY ticker, effective_date
                """
            ),
            {"tickers": tickers},
        ):
            yield dict(row._mapping)


# ---- nav_history (Futu rows only) ---------------------------------------

_WRITE_NAV = text(
    """
    INSERT INTO xenon.nav_history (
        broker, account_env, broker_account, date, nav, nav_type,
        total, cash, stock_value, options_value)
    VALUES (:broker, :env, :acc, :date, :nav, :nav_type,
            :total, :cash, :stock_value, 0)
    ON CONFLICT (broker, account_env, broker_account, date) DO UPDATE
       SET nav         = EXCLUDED.nav,
           nav_type    = EXCLUDED.nav_type,
           total       = EXCLUDED.total,
           cash        = EXCLUDED.cash,
           stock_value = EXCLUDED.stock_value
     WHERE xenon.nav_history.date = EXCLUDED.date
       AND xenon.nav_history.date = :upsert_today
    """
)


def write_nav_row(
    scope: AccountScope,
    recon_date: date,
    nav: Decimal,
    nav_type: str,
    cash: Decimal,
    stock_value: Decimal,
    is_today: bool = False,
) -> None:
    """Insert a NAV row. UPSERT only if recon_date == is_today flag.

    Historical rows (nav_type='reconstructed') are written once; the
    `:upsert_today` guard in the SQL prevents accidental mutation of
    past dates during daily syncs.
    """
    with get_sync_engine().begin() as conn:
        conn.execute(
            _WRITE_NAV,
            {
                **_scope_params(scope),
                "date": recon_date,
                "nav": nav,
                "nav_type": nav_type,
                "total": nav,
                "cash": cash,
                "stock_value": stock_value,
                "upsert_today": recon_date if is_today else date(1900, 1, 1),
            },
        )


def fetch_nav_history(scope: AccountScope, start: date, end: date) -> Iterator[dict]:
    with get_sync_engine().connect() as conn:
        for row in conn.execute(
            text(
                f"""
                SELECT date, nav, nav_type, total, cash, stock_value
                  FROM xenon.nav_history
                 WHERE {_scope_filter()}
                   AND date BETWEEN :start AND :end
                 ORDER BY date
                """
            ),
            {**_scope_params(scope), "start": start, "end": end},
        ):
            yield dict(row._mapping)


# ---- reconciliation log --------------------------------------------------

_WRITE_RECON_DAILY = text(
    """
    INSERT INTO xenon.futu_reconciliation_log (
        kind, recon_date, reported_nav, reconstructed_nav, delta,
        threshold, exceeded, notes, broker, account_env, broker_account)
    VALUES ('daily', :recon_date, :reported, :reconstructed, :delta,
            :threshold, :exceeded, CAST(:notes AS jsonb),
            :broker, :env, :acc)
    ON CONFLICT (broker, account_env, broker_account, recon_date)
    WHERE kind='daily'
    DO UPDATE SET
        reported_nav      = EXCLUDED.reported_nav,
        reconstructed_nav = EXCLUDED.reconstructed_nav,
        delta             = EXCLUDED.delta,
        threshold         = EXCLUDED.threshold,
        exceeded          = EXCLUDED.exceeded,
        notes             = EXCLUDED.notes,
        computed_at       = now()
    """
)


def write_recon_daily(
    scope: AccountScope,
    recon_date: date,
    reported: Decimal | None,
    reconstructed: Decimal | None,
    threshold: Decimal,
    notes: dict | None = None,
) -> None:
    delta = (reported - reconstructed) if (reported is not None and reconstructed is not None) else None
    exceeded = delta is not None and abs(delta) > threshold
    with get_sync_engine().begin() as conn:
        conn.execute(
            _WRITE_RECON_DAILY,
            {
                **_scope_params(scope),
                "recon_date": recon_date,
                "reported": reported,
                "reconstructed": reconstructed,
                "delta": delta,
                "threshold": threshold,
                "exceeded": exceeded,
                "notes": json.dumps(notes or {}, default=str),
            },
        )


_WRITE_RECON_ANOMALY = text(
    """
    INSERT INTO xenon.futu_reconciliation_log (
        kind, recon_date, notes, broker, account_env, broker_account)
    VALUES ('anomaly', :recon_date, CAST(:notes AS jsonb),
            :broker, :env, :acc)
    """
)


def write_recon_anomaly(scope: AccountScope, recon_date: date, notes: dict) -> None:
    with get_sync_engine().begin() as conn:
        conn.execute(
            _WRITE_RECON_ANOMALY,
            {
                **_scope_params(scope),
                "recon_date": recon_date,
                "notes": json.dumps(notes, default=str),
            },
        )
