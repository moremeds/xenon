"""Regression for snapshot-only orders.

When an order exists in IB (visible via `data/orders.json` snapshot) but was
not placed via the FastAPI flow, the orders_store DB has no row. Modifies
were rejected with `ORDER_NOT_FOUND` (404). `register_from_snapshot` is
called by the modify route to lazy-register such orders so the sequence
gate can apply, then the modify proceeds.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

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


def _fetch_one(sql: str, params: dict | None = None):
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    ).replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params or {}).fetchone()
    finally:
        engine.dispose()


def test_register_inserts_row_for_unknown_perm_id(db_path):
    """A perm_id absent from orders_store should be insertable from snapshot.

    Writes state=WORKING (IB returned this in get_open_orders, so it's
    literally working — `SUBMITTED` is unread and would render invisible).
    """
    inserted = register_from_snapshot(
        perm_id="1533567543",
        ib_order_id="-5",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=20,
        limit_price=1.7,
    )
    assert inserted["action"] == "INSERTED"

    # Row is now present and queryable by perm_id.
    row = _fetch_one(
        "SELECT ib_order_id, perm_id, ticker, action, quantity, limit_price, state, modify_sequence "
        "FROM xenon.order_submissions WHERE perm_id = :perm_id",
        {"perm_id": "1533567543"},
    )
    assert row is not None
    assert row[0] == "-5"
    assert row[1] == "1533567543"
    assert row[2] == "SPX"
    assert row[3] == "SELL"
    assert row[4] == 20
    assert float(row[5]) == 1.7
    assert row[6] == "WORKING"
    assert row[7] == 0


def test_register_persists_option_contract_fields(db_path):
    """Option / BAG snapshots must carry strike/right/expiry/con_id so the UI
    can render `SPY C500 2026-05-01` rather than bare `SPY`.
    """
    from datetime import date

    inserted = register_from_snapshot(
        perm_id="2000001",
        ib_order_id="100",
        ticker="SPY",
        security_type="OPT",
        action="BUY",
        quantity=2,
        limit_price=4.20,
        multiplier=100,
        strike=500.0,
        right="C",
        expiry="2026-05-01",
        con_id=987654321,
    )
    assert inserted["action"] == "INSERTED"

    row = _fetch_one(
        'SELECT strike, "right", expiry, con_id, multiplier FROM xenon.order_submissions WHERE perm_id = :perm_id',
        {"perm_id": "2000001"},
    )
    assert row is not None
    assert float(row[0]) == 500.0
    assert row[1] == "C"
    assert row[2] == date(2026, 5, 1)
    assert int(row[3]) == 987654321
    assert int(row[4]) == 100


def test_register_dedupes_against_existing_uuid_row(db_path):
    """When a Xenon-authored UUID row already exists for this perm_id+scope,
    register_from_snapshot must not insert a parallel `snapshot-<perm_id>`
    row. The existing row wins (it has richer authorship metadata).
    """
    from datetime import datetime, timezone

    from sqlalchemy import insert, select

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_submissions

    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id="uuid-existing-001",
                user_id="local",
                client_attempt_id="attempt-existing-001",
                ticker="QQQ",
                security_type="STK",
                action="BUY",
                quantity=10,
                multiplier=1,
                ib_order_id="42",
                perm_id="3000001",
                limit_price=400.0,
                state="WORKING",
                submitted_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
                updated_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
                modify_sequence=0,
                broker="IB",
                account_env="live",
                broker_account="U18007831",
            )
        )

    inserted = register_from_snapshot(
        perm_id="3000001",
        ib_order_id="42",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=10,
        limit_price=400.0,
        broker="IB",
        account_env="live",
        broker_account="U18007831",
    )
    assert inserted["action"] == "SKIPPED_UUID"

    with engine.connect() as conn:
        rows = conn.execute(
            select(order_submissions.c.submission_id).where(
                order_submissions.c.perm_id == "3000001",
                order_submissions.c.broker == "IB",
                order_submissions.c.account_env == "live",
                order_submissions.c.broker_account == "U18007831",
            )
        ).all()

    # Exactly one row, still keyed by the original UUID.
    assert len(rows) == 1
    assert rows[0].submission_id == "uuid-existing-001"


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
    )
    second = register_from_snapshot(
        perm_id="1533567543",
        ib_order_id="-5",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=20,
        limit_price=1.7,
    )
    assert first["action"] == "INSERTED"
    assert second["action"] == "NOOP"  # same values → no UPDATE, no event

    # Exactly one row.
    count = _fetch_one(
        "SELECT COUNT(*) FROM xenon.order_submissions WHERE perm_id = :perm_id",
        {"perm_id": "1533567543"},
    )[0]
    assert count == 1


def test_after_register_apply_modify_by_perm_id_succeeds(db_path):
    """End-to-end: register snapshot order, then sequence-gate apply succeeds."""
    # Pre-condition: unknown order returns -1 sentinel.
    pre = apply_modify_by_perm_id("1533567543", sequence=1)
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
    )

    post = apply_modify_by_perm_id("1533567543", sequence=1)
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
    )
    assert inserted["action"] == "INSERTED"


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
    )
    assert inserted["action"] == "INSERTED"
    mult = _fetch_one(
        "SELECT multiplier FROM xenon.order_submissions WHERE perm_id = :perm_id",
        {"perm_id": "1000"},
    )[0]
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
    )
    # apply_modify keys by ib_order_id — confirm the negative value round-trips.
    outcome = apply_modify("-5", sequence=1)
    assert outcome == {"applied": True, "current_sequence": 1}


def test_register_accepts_decimal_limit_price(db_path):
    """Decimal `limit_price` round-trips without float-noise.

    Floats can't represent 1.45 exactly (1.4499999999...), and the prior
    signature `limit_price: float` quietly accepted Decimal at runtime via
    duck-typing but converted via float() internally — re-introducing noise.
    """
    from decimal import Decimal

    register_from_snapshot(
        perm_id="9000001",
        ib_order_id="601",
        ticker="SPY",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("1.4567"),
    )
    row = _fetch_one(
        "SELECT limit_price FROM xenon.order_submissions WHERE perm_id = :perm_id",
        {"perm_id": "9000001"},
    )
    assert row is not None
    # DB column is Numeric(12,4) — exact Decimal round-trip.
    assert row[0] == Decimal("1.4567")


def test_register_accepts_decimal_strike(db_path):
    """Option `strike` accepts Decimal and persists exactly."""
    from decimal import Decimal

    register_from_snapshot(
        perm_id="9000002",
        ib_order_id="602",
        ticker="SPY",
        security_type="OPT",
        action="BUY",
        quantity=1,
        limit_price=Decimal("4.20"),
        multiplier=100,
        strike=Decimal("525.50"),
        right="C",
        expiry="2026-05-01",
    )
    row = _fetch_one(
        "SELECT strike FROM xenon.order_submissions WHERE perm_id = :perm_id",
        {"perm_id": "9000002"},
    )
    assert row is not None
    assert row[0] == Decimal("525.50")


def test_register_decimal_no_spurious_drift_on_repeat(db_path):
    """Re-registering the same Decimal must NOT trigger drift detection.

    Without Decimal-aware comparison, float round-trip noise can make the
    second call see `existing=1.4499999...` vs `incoming=1.45` and emit a
    spurious IB_MIRROR_UPDATE every poll tick.
    """
    from decimal import Decimal

    first = register_from_snapshot(
        perm_id="9000003",
        ib_order_id="603",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=10,
        limit_price=Decimal("1.45"),
    )
    second = register_from_snapshot(
        perm_id="9000003",
        ib_order_id="603",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=10,
        limit_price=Decimal("1.45"),
    )
    assert first["action"] == "INSERTED"
    assert second["action"] == "NOOP"
