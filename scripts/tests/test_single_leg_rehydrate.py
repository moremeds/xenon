"""Tests for F7.1 three-source reconcile helper (single_leg_rehydrate).

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §11.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from xenon.execution import orders_store, single_leg_rehydrate
from xenon.execution.orders_store import RequestRow, init_store, reserve_attempt
from xenon.execution.single_leg_rehydrate import (
    ReconcileDecision,
    _reconcile_from_three_sources,
    rehydrate_on_boot,
)

# ---------------------------------------------------------------------------
# Fake IB client
# ---------------------------------------------------------------------------


class FakeIBClient:
    def __init__(self, open_orders=None, executions=None, positions=None):
        self._open = open_orders or []
        self._execs = executions or []
        self._positions = positions or {}

    def get_open_orders(self):
        return self._open

    def get_executions(self, since=None):
        return self._execs

    def get_positions(self):
        return self._positions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(p))
    init_store(p)
    return p


def _pg_engine():
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    ).replace("postgresql+asyncpg://", "postgresql+psycopg://")
    return create_engine(url, pool_pre_ping=True)


def _make_working_row(
    db_path,
    *,
    ticker="SPY",
    perm_id="P-100",
    ib_order_id="11",
    security_type="OPT",
    action="BUY",
    quantity=1,
    state="WORKING",
    con_id=999,
    submitted_at: datetime | None = None,
):
    """Insert a row and advance it to WORKING (or leave PENDING)."""
    sid_outcome = reserve_attempt(
        user_id="u1",
        client_attempt_id=f"cai-{perm_id}",
        request=RequestRow(
            ticker=ticker,
            security_type=security_type,
            action=action,
            quantity=quantity,
            expiry="2026-05-16" if security_type == "OPT" else None,
            strike=Decimal("500") if security_type == "OPT" else None,
            right="C" if security_type == "OPT" else None,
            multiplier=100,
            con_id=con_id,
            limit_price=Decimal("1.50"),
        ),
        db_path=db_path,
    )
    engine = _pg_engine()
    try:
        with engine.begin() as con:
            params = {"submission_id": sid_outcome.submission_id}
            if state == "WORKING":
                con.execute(
                    text(
                        "UPDATE xenon.order_submissions "
                        "SET state='WORKING', ib_order_id=:ib_order_id, perm_id=:perm_id "
                        "WHERE submission_id=:submission_id"
                    ),
                    {**params, "ib_order_id": ib_order_id, "perm_id": perm_id},
                )
            elif state == "PENDING":
                if submitted_at is not None:
                    con.execute(
                        text(
                            "UPDATE xenon.order_submissions "
                            "SET submitted_at=:submitted_at WHERE submission_id=:submission_id"
                        ),
                        {**params, "submitted_at": submitted_at},
                    )
            else:
                con.execute(
                    text(
                        "UPDATE xenon.order_submissions "
                        "SET state=:state, ib_order_id=:ib_order_id, perm_id=:perm_id "
                        "WHERE submission_id=:submission_id"
                    ),
                    {**params, "state": state, "ib_order_id": ib_order_id, "perm_id": perm_id},
                )
    finally:
        engine.dispose()
    return sid_outcome.submission_id


def _fetch_row(db_path, submission_id):
    engine = _pg_engine()
    try:
        with engine.connect() as con:
            row = con.execute(
                text(
                    "SELECT state, filled_qty, avg_fill_price, reason_code, client_attempt_id "
                    "FROM xenon.order_submissions WHERE submission_id=:submission_id"
                ),
                {"submission_id": submission_id},
            ).fetchone()
    finally:
        engine.dispose()
    return row


def _fetch_events(db_path, submission_id):
    engine = _pg_engine()
    try:
        with engine.connect() as con:
            rows = con.execute(
                text(
                    'SELECT kind, detail FROM xenon.order_events '
                    'WHERE submission_id=:submission_id ORDER BY "at"'
                ),
                {"submission_id": submission_id},
            ).fetchall()
    finally:
        engine.dispose()
    return [(kind, detail if isinstance(detail, dict) else json.loads(detail) if detail else None) for kind, detail in rows]


# ---------------------------------------------------------------------------
# Pure helper tests (the ones the spec mandates)
# ---------------------------------------------------------------------------


def test_reconciles_working_order_still_open(db_path):
    sid = _make_working_row(db_path, perm_id="P-1", ib_order_id="11")
    ib = FakeIBClient(open_orders=[{"perm_id": "P-1", "status": "Submitted"}])

    rehydrate_on_boot(lambda: ib, orders_store, now=lambda: 1_000_000_000)

    state, filled_qty, avg, reason, _cai = _fetch_row(db_path, sid)
    assert state == "WORKING"
    assert filled_qty == 0
    events = _fetch_events(db_path, sid)
    assert any(k == "REHYDRATE_RECONCILED" for k, _ in events)
    reconciled = [d for k, d in events if k == "REHYDRATE_RECONCILED"][0]
    assert reconciled["from_state"] == "WORKING"
    assert reconciled["to_state"] == "WORKING"
    assert reconciled["sources"]["open_orders"] is True


def test_reconciles_filled_via_executions(db_path):
    sid = _make_working_row(db_path, perm_id="P-2", ib_order_id="12")
    ib = FakeIBClient(
        open_orders=[],
        executions=[{"perm_id": "P-2", "shares": 100, "avg_price": 1.50}],
    )

    rehydrate_on_boot(lambda: ib, orders_store, now=lambda: 1_000_000_000)

    state, filled_qty, avg_fill, _reason, _cai = _fetch_row(db_path, sid)
    assert state == "FILLED"
    assert filled_qty == 100
    assert Decimal(str(avg_fill)) == Decimal("1.50")
    events = _fetch_events(db_path, sid)
    assert any(k == "REHYDRATE_RECONCILED" for k, _ in events)


def test_reconciles_cancelled_positions_unchanged(db_path):
    sid = _make_working_row(db_path, perm_id="P-3", ib_order_id="13", con_id=777)
    # positions unchanged keyed by (ticker, con_id)
    ib = FakeIBClient(
        open_orders=[],
        executions=[],
        positions={("SPY", 777): {"changed": False}},
    )

    rehydrate_on_boot(lambda: ib, orders_store, now=lambda: 1_000_000_000)

    state, *_ = _fetch_row(db_path, sid)
    assert state == "CANCELLED"
    events = _fetch_events(db_path, sid)
    assert any(k == "REHYDRATE_RECONCILED" for k, _ in events)


def test_reconciles_unknown_positions_changed(db_path):
    sid = _make_working_row(db_path, perm_id="P-4", ib_order_id="14", con_id=888)
    # positions changed — means order likely filled but we missed the fill msg
    ib = FakeIBClient(
        open_orders=[],
        executions=[],
        positions={("SPY", 888): {"changed": True}},
    )

    rehydrate_on_boot(lambda: ib, orders_store, now=lambda: 1_000_000_000)

    state, *_ = _fetch_row(db_path, sid)
    # Never auto-CANCELLED when positions changed
    assert state == "UNKNOWN"
    events = _fetch_events(db_path, sid)
    assert any(k == "REHYDRATE_UNCERTAIN" for k, _ in events)


def test_pending_timeout_older_than_60s(db_path):
    old = datetime.now(timezone.utc) - timedelta(seconds=120)
    sid = _make_working_row(
        db_path,
        perm_id="P-5",
        ib_order_id=None,
        state="PENDING",
        submitted_at=old,
    )
    ib = FakeIBClient()

    rehydrate_on_boot(lambda: ib, orders_store)

    state, _filled, _avg, reason, cai = _fetch_row(db_path, sid)
    assert state == "FAILED"
    assert reason == "PENDING_TIMEOUT"
    assert cai == "cai-P-5"  # client_attempt_id retained


def test_pending_within_60s_untouched(db_path):
    young = datetime.now(timezone.utc) - timedelta(seconds=30)
    sid = _make_working_row(
        db_path,
        perm_id="P-6",
        ib_order_id=None,
        state="PENDING",
        submitted_at=young,
    )
    ib = FakeIBClient()

    rehydrate_on_boot(lambda: ib, orders_store)

    state, *_ = _fetch_row(db_path, sid)
    assert state == "PENDING"
    events = _fetch_events(db_path, sid)
    # No event for a no-op
    assert events == []


# ---------------------------------------------------------------------------
# Purity of the internal helper — no DB, no IB, no wall clock
# ---------------------------------------------------------------------------


def test_reconcile_helper_is_pure_working():
    row = {
        "submission_id": "s1",
        "state": "WORKING",
        "perm_id": "P",
        "ib_order_id": "1",
        "ticker": "SPY",
        "con_id": 1,
        "security_type": "OPT",
        "action": "BUY",
        "submitted_at": datetime.now(timezone.utc),
        "client_attempt_id": "c1",
    }
    d = _reconcile_from_three_sources(
        row,
        open_orders_by_perm={"P": {"perm_id": "P"}},
        execs_by_perm={},
        positions_changed=None,
        now=1_000_000_000,
    )
    assert isinstance(d, ReconcileDecision)
    assert d.to_state == "WORKING"
    assert d.event_kind == "REHYDRATE_RECONCILED"


def test_build_positions_snapshot_from_list():
    """A3: list input from ib.positions() is normalized to the snapshot dict shape."""
    from types import SimpleNamespace

    from xenon.execution.single_leg_rehydrate import _build_positions_snapshot

    rows = [
        {"ticker": "SPY", "con_id": 111},
        {"ticker": "QQQ", "con_id": 222},
    ]
    pos_list = [
        SimpleNamespace(contract=SimpleNamespace(symbol="SPY", conId=111)),
        SimpleNamespace(contract=SimpleNamespace(symbol="QQQ", conId=222)),
    ]
    snap = _build_positions_snapshot(pos_list, rows)
    assert isinstance(snap, dict)
    assert snap[("SPY", 111)] == {"changed": None}
    assert snap[("QQQ", 222)] == {"changed": None}


def test_build_positions_snapshot_passes_dict_through():
    """A3 back-compat: a dict input is returned unchanged."""
    from xenon.execution.single_leg_rehydrate import _build_positions_snapshot

    d = {("SPY", 999): {"changed": True}}
    assert _build_positions_snapshot(d, []) is d


def test_rehydrate_handles_list_positions_end_to_end(db_path):
    """A3: when get_positions() returns a list, rehydrate does not crash and
    routes no-baseline rows to UNKNOWN (REHYDRATE_UNCERTAIN)."""
    from types import SimpleNamespace

    sid = _make_working_row(db_path, perm_id="P-list", ib_order_id="33", con_id=500)
    ib = FakeIBClient(
        open_orders=[],
        executions=[],
        positions=[SimpleNamespace(contract=SimpleNamespace(symbol="SPY", conId=500))],
    )
    rehydrate_on_boot(lambda: ib, orders_store, now=lambda: 1_000_000_000)
    state, *_ = _fetch_row(db_path, sid)
    assert state == "UNKNOWN"
    events = _fetch_events(db_path, sid)
    assert any(k == "REHYDRATE_UNCERTAIN" for k, _ in events)


def test_submitted_at_epoch_is_utc_regardless_of_tz(db_path, monkeypatch):
    """A5: naive datetimes from legacy stores must be interpreted as UTC.

    Simulate a PST server and verify the epoch matches the UTC wall time,
    not offset by ±8h.
    """
    import os
    import time as time_mod

    from xenon.execution.single_leg_rehydrate import _submitted_at_epoch

    # Known UTC moment
    known = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    sid = _make_working_row(db_path, perm_id="P-utc", ib_order_id="44")
    engine = _pg_engine()
    try:
        with engine.begin() as con:
            con.execute(
                text(
                    "UPDATE xenon.order_submissions SET submitted_at=:submitted_at "
                    "WHERE submission_id=:submission_id"
                ),
                {"submitted_at": known.replace(tzinfo=None), "submission_id": sid},
            )
    finally:
        engine.dispose()

    naive_read_back = known.replace(tzinfo=None)

    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/Los_Angeles"
        if hasattr(time_mod, "tzset"):
            time_mod.tzset()
        epoch = _submitted_at_epoch(naive_read_back)
        assert abs(epoch - known.timestamp()) < 1.0
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        if hasattr(time_mod, "tzset"):
            time_mod.tzset()


def test_partially_filled_persists_fill_data(db_path):
    """A6: rehydrated PARTIALLY_FILLED must persist filled_qty + avg_fill_price.

    With a partial open-order status AND executions present, the reconcile
    decision carries fill data, and the dispatch routes through mark_terminal
    so those fields reach the DB.
    """
    sid = _make_working_row(db_path, perm_id="P42", ib_order_id="142")
    ib = FakeIBClient(
        open_orders=[{"perm_id": "P42", "status": "PartiallyFilled"}],
        executions=[{"perm_id": "P42", "shares": 50, "avg_price": 1.50}],
    )
    rehydrate_on_boot(lambda: ib, orders_store, now=lambda: 1_000_000_000)
    state, filled_qty, avg_fill, _reason, _cai = _fetch_row(db_path, sid)
    assert state == "PARTIALLY_FILLED"
    assert filled_qty == 50
    assert Decimal(str(avg_fill)) == Decimal("1.50")


def test_reconcile_helper_pending_timeout_pure():
    old = datetime.now(timezone.utc) - timedelta(seconds=120)
    row = {
        "submission_id": "s1",
        "state": "PENDING",
        "perm_id": None,
        "ib_order_id": None,
        "ticker": "SPY",
        "con_id": 1,
        "security_type": "OPT",
        "action": "BUY",
        "submitted_at": old,
        "client_attempt_id": "c1",
    }
    now_ts = datetime.now(timezone.utc).timestamp()
    d = _reconcile_from_three_sources(
        row,
        open_orders_by_perm={},
        execs_by_perm={},
        positions_changed=None,
        now=now_ts,
    )
    assert d.to_state == "FAILED"
    assert d.reason_code == "PENDING_TIMEOUT"
