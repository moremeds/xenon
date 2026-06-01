"""Verifies the Futu-history migration (2026_06_01_0001) creates all
expected tables, columns, view, and constraints.

Pre-req: `uv run alembic upgrade head` against `core_test`. The autouse
fixture `_postgres_orders_test_db` already handles truncation; this module
runs read-only schema introspection.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError


def test_futu_trades_table_exists(pg_test_engine):
    insp = inspect(pg_test_engine)
    cols = {c["name"] for c in insp.get_columns("futu_trades", schema="xenon")}
    assert {
        "id",
        "futu_deal_id",
        "futu_order_id",
        "ticker",
        "futu_code",
        "market",
        "action",
        "quantity",
        "price",
        "fees",
        "realized_pnl",
        "filled_at",
        "raw",
        "broker",
        "account_env",
        "broker_account",
    } <= cols


def test_futu_cash_flow_table_exists(pg_test_engine):
    insp = inspect(pg_test_engine)
    cols = {c["name"] for c in insp.get_columns("futu_cash_flow", schema="xenon")}
    assert {
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
        "raw",
    } <= cols


def test_corporate_actions_table_exists(pg_test_engine):
    insp = inspect(pg_test_engine)
    cols = {c["name"] for c in insp.get_columns("corporate_actions", schema="xenon")}
    assert {
        "ticker",
        "action_type",
        "effective_date",
        "ratio_numerator",
        "ratio_denominator",
        "new_ticker",
    } <= cols


def test_futu_reconciliation_log_table_exists(pg_test_engine):
    insp = inspect(pg_test_engine)
    cols = {c["name"] for c in insp.get_columns("futu_reconciliation_log", schema="xenon")}
    assert {
        "kind",
        "recon_date",
        "reported_nav",
        "reconstructed_nav",
        "delta",
        "threshold",
        "exceeded",
        "notes",
    } <= cols


def test_fx_daily_table_exists(pg_test_engine):
    insp = inspect(pg_test_engine)
    cols = {c["name"] for c in insp.get_columns("fx_daily", schema="xenon")}
    assert {"currency", "date", "currency_per_usd", "source"} <= cols


def test_nav_history_has_nav_type_column(pg_test_engine):
    """User decision (2026-06-01): rename source → nav_type, 4-value union."""
    insp = inspect(pg_test_engine)
    cols = {c["name"]: c for c in insp.get_columns("nav_history", schema="xenon")}
    assert "nav_type" in cols, "nav_history.nav_type column missing"
    assert "source" not in cols, "nav_history.source should have been renamed"
    assert cols["nav_type"]["nullable"] is False


def test_nav_type_constraint_allows_four_values(pg_test_engine):
    """Each of close/intraday/reported/reconstructed must be insertable."""
    rows = [
        ("FUTU", "live", "futu_acct_a", "2026-06-01", "close"),
        ("FUTU", "live", "futu_acct_a", "2026-06-02", "intraday"),
        ("FUTU", "live", "futu_acct_a", "2026-06-03", "reported"),
        ("FUTU", "live", "futu_acct_a", "2026-06-04", "reconstructed"),
    ]
    with pg_test_engine.begin() as conn:
        for broker, env, acct, d, nt in rows:
            conn.execute(
                text(
                    "INSERT INTO xenon.nav_history "
                    "(broker, account_env, broker_account, date, nav, nav_type) "
                    "VALUES (:b,:e,:a,:d,1000,:n)"
                ),
                {"b": broker, "e": env, "a": acct, "d": d, "n": nt},
            )


def test_nav_type_constraint_rejects_other_values(pg_test_engine):
    with pytest.raises(IntegrityError, match="ck_nav_history_nav_type"):
        with pg_test_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO xenon.nav_history "
                    "(broker, account_env, broker_account, date, nav, nav_type) "
                    "VALUES ('FUTU','live','futu_acct_b','2026-06-01',1000,'bogus')"
                )
            )


def test_all_fills_view_exists(pg_test_engine):
    with pg_test_engine.connect() as conn:
        result = conn.execute(
            text("SELECT count(*) FROM pg_views WHERE schemaname='xenon' AND viewname='all_fills'")
        ).scalar()
    assert result == 1


def test_futu_trades_ck_market_us_only(pg_test_engine):
    """The check constraint must reject non-US markets (Futu is US-only here)."""
    with pytest.raises(IntegrityError, match="ck_futu_trades_market_us"):
        with pg_test_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO xenon.futu_trades "
                    "(futu_deal_id, futu_order_id, ticker, futu_code, market, "
                    " action, quantity, price, filled_at, raw, "
                    " account_env, broker_account) "
                    "VALUES ('x','y','TENCENT','HK.00700','HK','BUY',100,300,"
                    "now(),'{}'::jsonb,'live','test_acct')"
                )
            )


def test_futu_trades_unique_scope_deal(pg_test_engine):
    """Same (broker, env, account, futu_deal_id) inserted twice must collide."""
    with pg_test_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO xenon.futu_trades "
                "(futu_deal_id, futu_order_id, ticker, futu_code, market, "
                " action, quantity, price, filled_at, raw, "
                " account_env, broker_account) "
                "VALUES ('deal-1','ord-1','AAPL','US.AAPL','US','BUY',10,180,"
                "now(),'{}'::jsonb,'live','futu_uniq_test')"
            )
        )
    with pytest.raises(IntegrityError, match="uq_futu_trades_scope_deal"):
        with pg_test_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO xenon.futu_trades "
                    "(futu_deal_id, futu_order_id, ticker, futu_code, market, "
                    " action, quantity, price, filled_at, raw, "
                    " account_env, broker_account) "
                    "VALUES ('deal-1','ord-1','AAPL','US.AAPL','US','BUY',10,180,"
                    "now(),'{}'::jsonb,'live','futu_uniq_test')"
                )
            )
