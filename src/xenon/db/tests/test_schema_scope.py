"""Verify broker/account_env/broker_account columns exist on scoped tables."""

from __future__ import annotations

import pytest

from xenon.db.schema import (
    account_snapshots,
    nav_history,
    order_submissions,
    positions,
    trades,
    wizard_combo_attempts,
    wizard_sessions,
)

SCOPED_TABLES = [
    order_submissions,
    trades,
    wizard_sessions,
    wizard_combo_attempts,
    positions,
    account_snapshots,
    nav_history,
]


@pytest.mark.parametrize("table", SCOPED_TABLES, ids=lambda t: t.name)
def test_scope_columns_exist(table):
    col_names = {c.name for c in table.columns}
    assert "broker" in col_names, f"{table.name} missing broker"
    assert "account_env" in col_names, f"{table.name} missing account_env"
    assert "broker_account" in col_names, f"{table.name} missing broker_account"


def test_nav_history_pk_is_scoped():
    pk_cols = [c.name for c in nav_history.primary_key.columns]
    assert pk_cols == ["broker", "account_env", "broker_account", "date"]


def test_order_idempotency_constraint_is_scoped():
    uq = None
    for c in order_submissions.constraints:
        if getattr(c, "name", None) == "uq_order_sub_user_attempt":
            uq = c
            break
    assert uq is not None
    col_names = [col.name for col in uq.columns]
    assert "broker" in col_names
    assert "account_env" in col_names
    assert "broker_account" in col_names
    assert "user_id" in col_names
    assert "client_attempt_id" in col_names
