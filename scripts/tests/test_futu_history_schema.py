"""Schema gate: xenon.futu_trades + xenon.futu_cash_flow.

Persisted-state tables that feed the backward NAV walk. Scope-keyed so
paper/live cannot bleed. Raw JSONB columns let us re-derive the curve from
source if we ever change the walk algorithm without re-pulling from Futu.
"""

from __future__ import annotations

import pytest

from xenon.db import schema


def test_futu_trades_table_exists():
    assert hasattr(schema, "futu_trades"), "xenon.futu_trades table must be defined in schema.py"


def test_futu_cash_flow_table_exists():
    assert hasattr(schema, "futu_cash_flow"), "xenon.futu_cash_flow table must be defined in schema.py"


@pytest.mark.parametrize(
    "col",
    [
        "broker",
        "account_env",
        "broker_account",
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
        "raw",
        "ingested_at",
    ],
)
def test_futu_trades_columns(col):
    cols = {c.name for c in schema.futu_trades.c}
    assert col in cols, f"futu_trades missing column {col!r}"


@pytest.mark.parametrize(
    "col",
    [
        "broker",
        "account_env",
        "broker_account",
        "futu_flow_id",
        "cashflow_type",
        "amount",
        "currency",
        "occurred_at",
        "raw",
        "ingested_at",
    ],
)
def test_futu_cash_flow_columns(col):
    cols = {c.name for c in schema.futu_cash_flow.c}
    assert col in cols, f"futu_cash_flow missing column {col!r}"


def test_futu_trades_pk_includes_scope_and_deal_id():
    pk_cols = [c.name for c in schema.futu_trades.primary_key.columns]
    assert pk_cols == ["broker", "account_env", "broker_account", "futu_deal_id"], (
        f"futu_trades PK must be (broker, account_env, broker_account, futu_deal_id); got {pk_cols}"
    )


def test_futu_cash_flow_pk_includes_scope_and_flow_id():
    pk_cols = [c.name for c in schema.futu_cash_flow.primary_key.columns]
    assert pk_cols == ["broker", "account_env", "broker_account", "futu_flow_id"], (
        f"futu_cash_flow PK must be (broker, account_env, broker_account, futu_flow_id); got {pk_cols}"
    )


def test_futu_trades_check_constraints_present():
    # CHECK constraints we rely on for v1: broker='FUTU', market='US', action in ('BUY','SELL')
    constraint_names = {c.name for c in schema.futu_trades.constraints if c.name}
    assert "ck_futu_trades_broker" in constraint_names
    assert "ck_futu_trades_market_us_only" in constraint_names
    assert "ck_futu_trades_action" in constraint_names


def test_futu_cash_flow_check_constraints_present():
    constraint_names = {c.name for c in schema.futu_cash_flow.constraints if c.name}
    assert "ck_futu_cash_flow_broker" in constraint_names
    assert "ck_futu_cash_flow_currency_usd_only" in constraint_names
    # cashflow_type is intentionally unrestricted — Futu returns an open enum
    # (Cash Dividend, Fund Subscription, Others, ...). v1 stores verbatim.
    assert "ck_futu_cash_flow_type" not in constraint_names
