"""Regression for snapshot-only orders.

When an order exists in IB (visible via `data/orders.json` snapshot) but was
not placed via the FastAPI flow, the orders_store DB has no row. Modifies
were rejected with `ORDER_NOT_FOUND` (404). `register_from_snapshot` is
called by the modify route to lazy-register such orders so the sequence
gate can apply, then the modify proceeds.
"""

from __future__ import annotations

import duckdb
import pytest

from xenon.execution.orders_store import (
    apply_modify,
    apply_modify_by_perm_id,
    init_store,
    register_from_snapshot,
)


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "orders.duckdb"
    init_store(path)
    return path


def test_register_inserts_row_for_unknown_perm_id(db_path):
    """A perm_id absent from orders_store should be insertable from snapshot."""
    inserted = register_from_snapshot(
        perm_id="1533567543",
        ib_order_id="-5",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=20,
        limit_price=1.7,
        db_path=db_path,
    )
    assert inserted is True

    # Row is now present and queryable by perm_id.
    con = duckdb.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT ib_order_id, perm_id, ticker, action, quantity, limit_price, state, modify_sequence "
            "FROM orders_submissions WHERE perm_id = ?",
            ["1533567543"],
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    assert row[0] == "-5"
    assert row[1] == "1533567543"
    assert row[2] == "SPX"
    assert row[3] == "SELL"
    assert row[4] == 20
    assert float(row[5]) == 1.7
    assert row[6] == "SUBMITTED"
    assert row[7] == 0


def test_register_is_idempotent(db_path):
    """Repeated calls with the same perm_id should not duplicate or error."""
    first = register_from_snapshot(
        perm_id="1533567543",
        ib_order_id="-5",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=20,
        limit_price=1.7,
        db_path=db_path,
    )
    second = register_from_snapshot(
        perm_id="1533567543",
        ib_order_id="-5",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=20,
        limit_price=1.7,
        db_path=db_path,
    )
    assert first is True
    assert second is False  # already exists, no-op

    # Exactly one row.
    con = duckdb.connect(str(db_path))
    try:
        count = con.execute(
            "SELECT COUNT(*) FROM orders_submissions WHERE perm_id = ?",
            ["1533567543"],
        ).fetchone()[0]
    finally:
        con.close()
    assert count == 1


def test_after_register_apply_modify_by_perm_id_succeeds(db_path):
    """End-to-end: register snapshot order, then sequence-gate apply succeeds."""
    # Pre-condition: unknown order returns -1 sentinel.
    pre = apply_modify_by_perm_id("1533567543", sequence=1, db_path=db_path)
    assert pre == {"applied": False, "current_sequence": -1}

    # Register, then retry.
    register_from_snapshot(
        perm_id="1533567543",
        ib_order_id="-5",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=20,
        limit_price=1.7,
        db_path=db_path,
    )

    post = apply_modify_by_perm_id("1533567543", sequence=1, db_path=db_path)
    assert post == {"applied": True, "current_sequence": 1}


def test_register_works_for_stock_security_type(db_path):
    """STK orders should register the same way."""
    inserted = register_from_snapshot(
        perm_id="999",
        ib_order_id="42",
        ticker="TSLA",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=350.0,
        db_path=db_path,
    )
    assert inserted is True


def test_register_works_for_single_leg_option(db_path):
    """OPT orders should register, including multiplier override."""
    inserted = register_from_snapshot(
        perm_id="1000",
        ib_order_id="43",
        ticker="AAPL",
        security_type="OPT",
        action="SELL",
        quantity=5,
        limit_price=6.50,
        multiplier=100,
        db_path=db_path,
    )
    assert inserted is True
    con = duckdb.connect(str(db_path))
    try:
        mult = con.execute(
            "SELECT multiplier FROM orders_submissions WHERE perm_id = ?",
            ["1000"],
        ).fetchone()[0]
    finally:
        con.close()
    assert mult == 100


def test_negative_ib_order_id_accepted(db_path):
    """Synthetic negative ib_order_id (from snapshot reconstruction) is preserved as-is."""
    register_from_snapshot(
        perm_id="1533567543",
        ib_order_id="-5",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=20,
        limit_price=1.7,
        db_path=db_path,
    )
    # apply_modify keys by ib_order_id — confirm the negative value round-trips.
    outcome = apply_modify("-5", sequence=1, db_path=db_path)
    assert outcome == {"applied": True, "current_sequence": 1}
