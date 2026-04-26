"""Verify broker/account_env/broker_account columns exist on scoped tables."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
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


@pytest.fixture
def clean_order_submissions(monkeypatch):
    """Point sync engine at the test DB and truncate order_submissions."""
    import os

    test_url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    ).replace("postgresql+asyncpg://", "postgresql+psycopg://")
    monkeypatch.setenv("DATABASE_URL", test_url)
    import xenon.db.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_sync_engine", None)

    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE xenon.order_events CASCADE"))
        conn.execute(text("TRUNCATE TABLE xenon.order_submissions CASCADE"))
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE xenon.order_events CASCADE"))
        conn.execute(text("TRUNCATE TABLE xenon.order_submissions CASCADE"))


def test_paper_live_orders_do_not_collide(clean_order_submissions):
    """Same user + client_attempt_id in paper and live must create 2 rows."""
    from xenon.execution.orders_store import RequestRow, reserve_attempt

    req = RequestRow(
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        multiplier=1,
        limit_price=Decimal("150.00"),
    )
    r1 = reserve_attempt(
        "local",
        "cid-001",
        req,
        broker="IB",
        account_env="paper",
        broker_account="DU1111111",
    )
    assert r1.status == "winner"

    r2 = reserve_attempt(
        "local",
        "cid-001",
        req,
        broker="IB",
        account_env="live",
        broker_account="U2222222",
    )
    assert r2.status == "winner"
    assert r1.submission_id != r2.submission_id
