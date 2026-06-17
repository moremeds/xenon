"""Schema definitions for the Futu order-querying tables.

Asserts against `xenon_metadata` (pure Python — no DB connection, so this is
safe regardless of which DATABASE_URL is configured). The live migration is
verified separately via `alembic upgrade head` against core_test.
"""

from __future__ import annotations

import pytest

from xenon.db.schema import xenon_metadata


def _table(name: str):
    return xenon_metadata.tables[f"xenon.{name}"]


@pytest.mark.parametrize("table", ["futu_orders", "futu_order_fees", "futu_closed_trades"])
def test_futu_order_tables_defined(table):
    assert f"xenon.{table}" in xenon_metadata.tables


def test_futu_orders_columns():
    cols = {c.name for c in _table("futu_orders").columns}
    expected = {
        "broker",
        "account_env",
        "broker_account",
        "futu_order_id",
        "ticker",
        "futu_code",
        "market",
        "action",
        "order_type",
        "quantity",
        "limit_price",
        "aux_price",
        "status",
        "tif",
        "filled_qty",
        "avg_fill_price",
        "created_at",
        "updated_at",
        "raw",
        "ingested_at",
    }
    assert expected <= cols


def test_futu_orders_pk_and_checks():
    t = _table("futu_orders")
    pk = {c.name for c in t.primary_key.columns}
    assert pk == {"broker", "account_env", "broker_account", "futu_order_id"}
    check_names = {c.name for c in t.constraints if c.__class__.__name__ == "CheckConstraint"}
    assert "ck_futu_orders_broker" in check_names
    assert "ck_futu_orders_action" in check_names


def test_futu_closed_trades_columns():
    cols = {c.name for c in _table("futu_closed_trades").columns}
    expected = {
        "broker",
        "account_env",
        "broker_account",
        "futu_close_id",
        "ticker",
        "futu_code",
        "structure",
        "action",
        "quantity",
        "entry_cost",
        "exit_cost",
        "realized_pnl",
        "cost_basis",
        "proceeds",
        "opened_at",
        "closed_at",
        "metadata",
        "ingested_at",
    }
    assert expected <= cols


def test_futu_order_fees_columns():
    cols = {c.name for c in _table("futu_order_fees").columns}
    assert {"broker", "account_env", "broker_account", "futu_order_id", "total_fee", "currency", "raw"} <= cols


def test_journal_has_futu_close_id_column_and_dedup_index():
    journal = _table("journal_entries")
    assert "futu_close_id" in {c.name for c in journal.columns}
    index_names = {ix.name for ix in journal.indexes}
    assert "uq_journal_futu_auto_import" in index_names
