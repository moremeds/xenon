"""Tests for orders_store.apply_modify monotonic modify_sequence gate (F5.3)."""

from __future__ import annotations

from decimal import Decimal

import duckdb
import pytest

from xenon.execution import orders_store
from xenon.execution.orders_store import (
    RequestRow,
    apply_modify,
    init_store,
    mark_submitted,
    reserve_attempt,
)


def _req(**over) -> RequestRow:
    base = dict(
        ticker="SPY",
        security_type="STK",
        action="BUY",
        quantity=100,
        expiry=None,
        strike=None,
        right=None,
        multiplier=100,
        con_id=756733,
        limit_price=Decimal("500.15"),
    )
    base.update(over)
    return RequestRow(**base)


def _seed_order(db_path, *, user_id: str, client_attempt_id: str, ib_order_id: str) -> None:
    outcome = reserve_attempt(user_id, client_attempt_id, _req(), db_path=db_path)
    mark_submitted(
        submission_id=outcome.submission_id,
        ib_order_id=ib_order_id,
        perm_id=None,
        placing_client_id=1,
        db_path=db_path,
    )


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "orders.duckdb"
    init_store(path)
    return path


def test_applies_monotonic_modify_sequence(db_path):
    _seed_order(db_path, user_id="u1", client_attempt_id="a1", ib_order_id="1")

    first = apply_modify(order_id="1", sequence=2, db_path=db_path)
    assert first == {"applied": True, "current_sequence": 2}

    second = apply_modify(order_id="1", sequence=2, db_path=db_path)
    assert second == {"applied": False, "current_sequence": 2}


def test_modify_sequence_resets_per_order_id(db_path):
    _seed_order(db_path, user_id="u1", client_attempt_id="a1", ib_order_id="1")
    _seed_order(db_path, user_id="u1", client_attempt_id="a2", ib_order_id="2")

    out = apply_modify(order_id="1", sequence=5, db_path=db_path)
    assert out == {"applied": True, "current_sequence": 5}

    # order 2 unaffected
    out2 = apply_modify(order_id="2", sequence=1, db_path=db_path)
    assert out2 == {"applied": True, "current_sequence": 1}

    # verify via read that order 1 is still at 5
    con = duckdb.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT modify_sequence FROM orders_submissions WHERE ib_order_id = ?",
            ["1"],
        ).fetchone()
    finally:
        con.close()
    assert row[0] == 5


def test_modify_sequence_monotonic_strictly_increasing(db_path):
    _seed_order(db_path, user_id="u1", client_attempt_id="a1", ib_order_id="1")

    assert apply_modify(order_id="1", sequence=1, db_path=db_path) == {
        "applied": True,
        "current_sequence": 1,
    }
    assert apply_modify(order_id="1", sequence=1, db_path=db_path) == {
        "applied": False,
        "current_sequence": 1,
    }
    assert apply_modify(order_id="1", sequence=2, db_path=db_path) == {
        "applied": True,
        "current_sequence": 2,
    }
    assert apply_modify(order_id="1", sequence=2, db_path=db_path) == {
        "applied": False,
        "current_sequence": 2,
    }


def test_apply_modify_unknown_order_id(db_path):
    out = apply_modify(order_id="999", sequence=1, db_path=db_path)
    assert out == {"applied": False, "current_sequence": -1}


def test_migration_idempotent(db_path):
    # Second init_store call on the same path must not error (ADD COLUMN IF NOT EXISTS).
    init_store(db_path)
    init_store(db_path)

    # verify schema has modify_sequence column
    con = duckdb.connect(str(db_path))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info('orders_submissions')").fetchall()]
    finally:
        con.close()
    assert "modify_sequence" in cols
