"""Tests for F7.1 three-source reconcile helper (single_leg_rehydrate).

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §11.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

# Phase 2 carve-out: this module's tests open their own SQLAlchemy engine
# (helpers calling sqlalchemy.create_engine directly, or subprocess CLIs)
# and therefore can't share the test's BEGIN/ROLLBACK transaction. They
# stay on Phase 1 TRUNCATE pre+post isolation via this marker. Migration
# to txn-rollback would require refactoring those local engine helpers to
# go through xenon.db.engine.get_sync_engine().
pytestmark = pytest.mark.committed_db

from sqlalchemy import create_engine, text

from xenon.execution import orders_store, single_leg_rehydrate
from xenon.execution.orders_store import RequestRow, init_store, reserve_attempt
from xenon.execution.single_leg_rehydrate import (
    ReconcileDecision,
    _index_executions,
    _reconcile_from_three_sources,
    rehydrate_on_boot,
)

# ---------------------------------------------------------------------------
# Fake IB client
# ---------------------------------------------------------------------------


class FakeIBClient:
    def __init__(self, open_orders=None, executions=None, positions=None, qualify_lookup=None):
        self._open = open_orders or []
        self._execs = executions or []
        self._positions = positions or {}
        # Map conId -> SimpleNamespace(strike, right, lastTradeDateOrContractMonth, secType).
        # Lets tests exercise the leg-metadata enrichment path.
        self._qualify_lookup = qualify_lookup or {}

    def get_open_orders(self):
        return self._open

    def get_executions(self, since=None):
        return self._execs

    def get_positions(self):
        return self._positions

    def qualify_contracts(self, contract):
        con_id = getattr(contract, "conId", None)
        match = self._qualify_lookup.get(con_id)
        return [match] if match is not None else []


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
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
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
        broker=broker,
        account_env=account_env,
        broker_account=broker_account,
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


def _fetch_submission_summary(submission_id: str):
    engine = _pg_engine()
    try:
        with engine.connect() as con:
            return con.execute(
                text(
                    "SELECT state, filled_qty, avg_fill_price "
                    "FROM xenon.order_submissions WHERE submission_id=:submission_id"
                ),
                {"submission_id": submission_id},
            ).fetchone()
    finally:
        engine.dispose()


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
                text('SELECT kind, detail FROM xenon.order_events WHERE submission_id=:submission_id ORDER BY "at"'),
                {"submission_id": submission_id},
            ).fetchall()
    finally:
        engine.dispose()
    return [
        (kind, detail if isinstance(detail, dict) else json.loads(detail) if detail else None) for kind, detail in rows
    ]


def _fetch_fill_rows():
    engine = _pg_engine()
    try:
        with engine.connect() as con:
            return con.execute(
                text(
                    "SELECT exec_id, submission_id, ticker, side, qty, price, metadata, broker, account_env, broker_account "
                    "FROM xenon.order_fills ORDER BY exec_id"
                )
            ).fetchall()
    finally:
        engine.dispose()


def _fetch_trade_rows():
    engine = _pg_engine()
    try:
        with engine.connect() as con:
            return con.execute(
                text(
                    "SELECT submission_id, ticker, action, quantity, entry_cost, state, metadata "
                    "FROM xenon.trades ORDER BY id"
                )
            ).fetchall()
    finally:
        engine.dispose()


def _fetch_outbox(channel: str):
    engine = _pg_engine()
    try:
        with engine.connect() as con:
            return con.execute(
                text("SELECT channel, source, payload FROM events.outbox WHERE channel=:channel ORDER BY id"),
                {"channel": channel},
            ).fetchall()
    finally:
        engine.dispose()


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


def test_rehydrate_records_fill_and_trade_for_explicit_scope(db_path):
    sid = _make_working_row(
        db_path,
        ticker="AAPL",
        perm_id="P-fill",
        ib_order_id="42",
        security_type="STK",
        quantity=100,
        con_id=265598,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )
    ib = FakeIBClient(
        open_orders=[],
        executions=[
            {
                "exec_id": "exec-single-fill-1",
                "perm_id": "P-fill",
                "ib_order_id": "42",
                "con_id": 265598,
                "ticker": "AAPL",
                "side": "BOT",
                "shares": 100,
                "avg_price": 190.25,
                "commission": 1.25,
                "time": datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc),
            }
        ],
    )

    rehydrate_on_boot(
        lambda: ib,
        orders_store,
        now=lambda: 1_000_000_000,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )

    fill_rows = _fetch_fill_rows()
    assert len(fill_rows) == 1
    fill = fill_rows[0]
    assert fill.exec_id == "exec-single-fill-1"
    assert fill.submission_id == sid
    assert fill.side == "BUY"
    assert fill.qty == 100
    assert Decimal(str(fill.price)) == Decimal("190.2500")
    assert fill.account_env == "paper"
    assert fill.broker_account == "DU123456"

    trade_rows = _fetch_trade_rows()
    assert len(trade_rows) == 1
    trade = trade_rows[0]
    assert trade.submission_id == sid
    assert trade.ticker == "AAPL"
    assert trade.action == "BUY"
    assert trade.quantity == 100
    assert Decimal(str(trade.entry_cost)) == Decimal("19026.2500")
    assert trade.state == "OPEN"

    fill_events = _fetch_outbox("fill.recorded")
    assert len(fill_events) == 1
    assert fill_events[0].payload["exec_id"] == "exec-single-fill-1"


def test_rehydrate_bag_execution_uses_envelope_for_quantity_and_preserves_leg_metadata(db_path):
    sid = _make_working_row(
        db_path,
        ticker="SPX",
        perm_id="P-bag-fill",
        ib_order_id="21",
        security_type="BAG",
        action="SELL",
        quantity=11,
        con_id=28812380,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )
    filled_at = datetime(2026, 4, 29, 13, 45, 20, tzinfo=timezone.utc)
    ib = FakeIBClient(
        open_orders=[],
        executions=[
            {
                "exec_id": "bag-parent",
                "perm_id": "P-bag-fill",
                "ib_order_id": "21",
                "con_id": 28812380,
                "ticker": "SPX",
                "sec_type": "BAG",
                "side": "SLD",
                "shares": 11,
                "avg_price": Decimal("1.40"),
                "time": filled_at,
            },
            {
                "exec_id": "bag-leg-short",
                "perm_id": "P-bag-fill",
                "ib_order_id": "21",
                "con_id": 872609959,
                "ticker": "SPX",
                "sec_type": "OPT",
                "right": "P",
                "strike": Decimal("5600"),
                "expiry": "20260429",
                "side": "SLD",
                "shares": 11,
                "avg_price": Decimal("27.90"),
                "commission": Decimal("14.1950"),
                "realized_pnl": Decimal("-29431.38995"),
                "time": filled_at,
            },
            {
                "exec_id": "bag-leg-long",
                "perm_id": "P-bag-fill",
                "ib_order_id": "21",
                "con_id": 873604441,
                "ticker": "SPX",
                "sec_type": "OPT",
                "right": "P",
                "strike": Decimal("5595"),
                "expiry": "20260429",
                "side": "BOT",
                "shares": 11,
                "avg_price": Decimal("26.50"),
                "commission": Decimal("14.1950"),
                "realized_pnl": Decimal("29154.61005"),
                "time": filled_at,
            },
        ],
    )

    rehydrate_on_boot(
        lambda: ib,
        orders_store,
        now=lambda: 1_000_000_000,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )

    summary = _fetch_submission_summary(sid)
    assert summary.state == "FILLED"
    assert summary.filled_qty == 11
    assert Decimal(str(summary.avg_fill_price)) == Decimal("1.4000")

    fills = {row.exec_id: row for row in _fetch_fill_rows()}
    assert fills["bag-parent"].metadata["sec_type"] == "BAG"
    assert fills["bag-leg-short"].metadata["sec_type"] == "OPT"
    assert fills["bag-leg-short"].metadata["realized_pnl"] == "-29431.38995"
    assert fills["bag-leg-long"].metadata["right"] == "P"


def test_rehydrate_qualifies_leg_contracts_when_metadata_missing(db_path):
    """When IB returns leg-level fills with a contract that lacks strike/
    right/expiry (only conId reliable), the rehydrate must qualify each
    conId via IB so the persisted leg metadata carries the full contract.

    Without this fix the blotter renders 'Bull Put Spread (Short Unknown
    Unknown / Long Unknown Unknown)' for any combo whose close happened
    while Xenon was down.
    """
    sid = _make_working_row(
        db_path,
        ticker="SPX",
        perm_id="P-bag-enrich",
        ib_order_id="0",
        security_type="BAG",
        action="SELL",
        quantity=11,
        con_id=28812380,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )
    filled_at = datetime(2026, 4, 29, 13, 45, 20, tzinfo=timezone.utc)
    ib = FakeIBClient(
        open_orders=[],
        executions=[
            {
                "exec_id": "bag-env",
                "perm_id": "P-bag-enrich",
                "ib_order_id": "0",
                "con_id": 28812380,
                "ticker": "SPX",
                "sec_type": "BAG",
                "side": "SLD",
                "shares": 11,
                "avg_price": Decimal("1.40"),
                "time": filled_at,
            },
            {
                "exec_id": "leg-short-incomplete",
                "perm_id": "P-bag-enrich",
                "ib_order_id": "0",
                "con_id": 872609959,
                "ticker": "SPX",
                "side": "SLD",
                "shares": 11,
                "avg_price": Decimal("27.90"),
                "commission": Decimal("14.1950"),
                "realized_pnl": Decimal("-29431.38995"),
                "time": filled_at,
                # NB: no sec_type/strike/right/expiry — Fill.contract was incomplete
            },
            {
                "exec_id": "leg-long-incomplete",
                "perm_id": "P-bag-enrich",
                "ib_order_id": "0",
                "con_id": 873604441,
                "ticker": "SPX",
                "side": "BOT",
                "shares": 11,
                "avg_price": Decimal("26.50"),
                "commission": Decimal("14.1950"),
                "realized_pnl": Decimal("29154.61005"),
                "time": filled_at,
            },
        ],
        qualify_lookup={
            872609959: SimpleNamespace(
                conId=872609959,
                symbol="SPX",
                strike=7070.0,
                right="P",
                lastTradeDateOrContractMonth="20260501",
                secType="OPT",
            ),
            873604441: SimpleNamespace(
                conId=873604441,
                symbol="SPX",
                strike=7065.0,
                right="P",
                lastTradeDateOrContractMonth="20260501",
                secType="OPT",
            ),
        },
    )

    rehydrate_on_boot(
        lambda: ib,
        orders_store,
        now=lambda: 1_000_000_000,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )

    fills = {row.exec_id: row for row in _fetch_fill_rows()}
    short_meta = fills["leg-short-incomplete"].metadata
    long_meta = fills["leg-long-incomplete"].metadata
    assert short_meta.get("strike") == "7070.0"
    assert short_meta.get("right") == "P"
    assert short_meta.get("expiry") == "20260501"
    assert long_meta.get("strike") == "7065.0"
    assert long_meta.get("right") == "P"
    assert long_meta.get("expiry") == "20260501"


def test_rehydrate_replay_does_not_duplicate_fill_or_trade(db_path):
    _make_working_row(
        db_path,
        ticker="AAPL",
        perm_id="P-replay",
        ib_order_id="43",
        security_type="STK",
        quantity=10,
        con_id=265598,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )
    ib = FakeIBClient(
        open_orders=[],
        executions=[
            {
                "exec_id": "exec-single-replay",
                "perm_id": "P-replay",
                "ib_order_id": "43",
                "con_id": 265598,
                "ticker": "AAPL",
                "side": "BUY",
                "shares": 10,
                "avg_price": 20.00,
                "time": datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc),
            }
        ],
    )

    kwargs = {
        "ib_client_factory": lambda: ib,
        "orders_store": orders_store,
        "now": lambda: 1_000_000_000,
        "broker": "IB",
        "account_env": "paper",
        "broker_account": "DU123456",
    }
    rehydrate_on_boot(**kwargs)
    rehydrate_on_boot(**kwargs)

    assert len(_fetch_fill_rows()) == 1
    assert len(_fetch_trade_rows()) == 1
    assert len(_fetch_outbox("fill.recorded")) == 1


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


def test_no_op_when_open_orders_empty_and_no_positions_baseline(db_path):
    """Boot-time rehydrate must not demote rows when it has no signal.

    Regression: snapshot-* rows imported by the IB-side importer in state
    WORKING were getting transitioned to UNKNOWN on every server restart.
    Decision tree: open_orders is empty (transient race during boot, or
    IB Gateway just connected and hasn't fully populated) + no positions
    baseline → previous code routed to UNKNOWN. Correct behavior is to
    leave state alone — the next sync cycle has the authoritative signal.

    positions_changed=True (genuine state change) still routes to UNKNOWN
    per `test_reconciles_unknown_positions_changed`. positions_changed=False
    still routes to CANCELLED. Only the None branch (no baseline) is a no-op.
    """
    sid = _make_working_row(db_path, perm_id="P-no-baseline", ib_order_id="42", con_id=4242)
    # No baseline → positions list with no `changed` markers. open_orders empty.
    ib = FakeIBClient(open_orders=[], executions=[], positions=[])

    rehydrate_on_boot(lambda: ib, orders_store, now=lambda: 1_000_000_000)

    state, *_ = _fetch_row(db_path, sid)
    assert state == "WORKING", f"expected WORKING (no-op on no-baseline), got {state}"
    events = _fetch_events(db_path, sid)
    # No event emitted on a no-op — same as test_pending_within_60s_untouched.
    assert events == [], f"expected no events on no-op, got {events}"


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


def test_index_executions_reads_ib_fill_objects():
    fill = SimpleNamespace(
        execution=SimpleNamespace(permId=123, shares=2, avgPrice=1.25),
        contract=SimpleNamespace(symbol="SPY", conId=999),
    )

    indexed = _index_executions([fill])

    assert indexed == {"123": {"shares": 2, "avg_price": 1.25}}


def test_normalize_execution_record_preserves_fractional_shares():
    """Boot rehydrate is a sibling path into orders_store.record_fill. Like the
    ib_reconcile fix, fractional-share executions (recurring QQQ/SPY buys) must
    reach record_fill as a Decimal — int(shares) would truncate 0.4977 to 0,
    re-creating the qty=0 blotter bug via a different code path."""
    from xenon.execution.single_leg_rehydrate import _normalize_execution_record

    # dict branch
    dict_rec = _normalize_execution_record({"perm_id": 1, "shares": "0.4977", "avg_price": "500.10", "side": "BUY"})
    assert dict_rec is not None
    assert dict_rec["qty"] == Decimal("0.4977")

    # ib_async object branch
    obj_rec = _normalize_execution_record(
        SimpleNamespace(
            execution=SimpleNamespace(permId=2, shares=0.5023, avgPrice=500.10),
            contract=SimpleNamespace(symbol="QQQ", conId=1),
            commissionReport=None,
        )
    )
    assert obj_rec is not None
    assert obj_rec["qty"] == Decimal("0.5023")


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
    """A3: when get_positions() returns a list, rehydrate does not crash.

    Updated contract: rows without a positions baseline are now NO-OPs (state
    preserved, no event emitted) — see test_no_op_when_open_orders_empty_and_
    no_positions_baseline above and the REHYDRATE_NO_BASELINE branch in
    _reconcile_from_three_sources. Previously this routed to UNKNOWN, which
    silently demoted live `snapshot-*` rows on every boot.
    """
    from types import SimpleNamespace

    sid = _make_working_row(db_path, perm_id="P-list", ib_order_id="33", con_id=500)
    ib = FakeIBClient(
        open_orders=[],
        executions=[],
        positions=[SimpleNamespace(contract=SimpleNamespace(symbol="SPY", conId=500))],
    )
    rehydrate_on_boot(lambda: ib, orders_store, now=lambda: 1_000_000_000)
    state, *_ = _fetch_row(db_path, sid)
    assert state == "WORKING", f"expected WORKING (no-op on no-baseline), got {state}"
    events = _fetch_events(db_path, sid)
    assert events == [], f"expected no events on no-op, got {events}"


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
                    "UPDATE xenon.order_submissions SET submitted_at=:submitted_at WHERE submission_id=:submission_id"
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
