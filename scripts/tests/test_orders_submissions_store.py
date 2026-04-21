import threading
from decimal import Decimal

import duckdb
import pytest

from xenon.execution import orders_store
from xenon.execution.orders_store import (
    RequestRow,
    ReservationOutcome,
    init_store,
    reserve_attempt,
)


def _req(**over) -> RequestRow:
    base = dict(
        ticker="SPY",
        security_type="STK",
        action="SELL",
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


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(p))
    return p


def test_init_store_creates_tables_and_indexes(db_path):
    orders_store.init_store(db_path)

    con = duckdb.connect(str(db_path))
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert {"orders_submissions", "orders_events"} <= tables

    cols = {r[1] for r in con.execute("PRAGMA table_info('orders_submissions')").fetchall()}
    expected = {
        "submission_id",
        "user_id",
        "client_attempt_id",
        "ticker",
        "security_type",
        "action",
        "quantity",
        "expiry",
        "strike",
        "right",
        "multiplier",
        "con_id",
        "placing_client_id",
        "ib_order_id",
        "perm_id",
        "limit_price",
        "state",
        "reason_code",
        "filled_qty",
        "avg_fill_price",
        "submitted_at",
        "updated_at",
    }
    assert expected <= cols, f"missing cols: {expected - cols}"


def test_init_store_is_idempotent(db_path):
    orders_store.init_store(db_path)
    orders_store.init_store(db_path)  # must not raise


def test_reserve_attempt_winner(db_path):
    init_store(db_path)
    out = reserve_attempt(
        user_id="local",
        client_attempt_id="cid-A",
        request=_req(),
        db_path=db_path,
    )
    assert out.status == "winner"
    assert out.submission_id
    assert out.duplicate_of is None
    assert out.state == "PENDING"


def test_reserve_attempt_loser_non_terminal(db_path):
    init_store(db_path)
    first = reserve_attempt("local", "cid-B", _req(), db_path=db_path)
    second = reserve_attempt("local", "cid-B", _req(), db_path=db_path)
    assert first.status == "winner"
    assert second.status == "duplicate"
    assert second.submission_id == first.submission_id
    assert second.state == "PENDING"
    assert second.duplicate_of is None


def test_reserve_attempt_concurrent_only_one_winner(db_path):
    init_store(db_path)
    outcomes: list[ReservationOutcome] = []
    barrier = threading.Barrier(8)

    def _go():
        barrier.wait()
        outcomes.append(
            reserve_attempt("local", "cid-C", _req(), db_path=db_path)
        )

    threads = [threading.Thread(target=_go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [o for o in outcomes if o.status == "winner"]
    dupes = [o for o in outcomes if o.status == "duplicate"]
    assert len(winners) == 1
    assert len(dupes) == 7
    assert all(d.submission_id == winners[0].submission_id for d in dupes)


from xenon.execution.orders_store import (
    lookup_by_attempt,
    mark_submitted,
    mark_terminal,
    record_event,
)


def test_mark_submitted_stamps_ib_order_id(db_path):
    init_store(db_path)
    win = reserve_attempt("local", "cid-S", _req(), db_path=db_path)
    mark_submitted(
        submission_id=win.submission_id,
        ib_order_id="5001",
        perm_id="9901",
        placing_client_id=26,
        db_path=db_path,
    )
    row = lookup_by_attempt("local", "cid-S", db_path=db_path)
    assert row.ib_order_id == "5001"
    assert row.perm_id == "9901"
    assert row.placing_client_id == 26
    assert row.state == "WORKING"


def test_mark_terminal_sets_reason_code(db_path):
    init_store(db_path)
    win = reserve_attempt("local", "cid-T", _req(), db_path=db_path)
    mark_terminal(
        submission_id=win.submission_id,
        state="REJECTED",
        reason_code="IB_REJECT_201",
        filled_qty=0,
        avg_fill_price=None,
        db_path=db_path,
    )
    row = lookup_by_attempt("local", "cid-T", db_path=db_path)
    assert row.state == "REJECTED"
    assert row.reason_code == "IB_REJECT_201"

    out = reserve_attempt("local", "cid-T", _req(), db_path=db_path)
    assert out.status == "terminal"
    assert out.reason_code == "IB_REJECT_201"


def test_record_event_appends_row(db_path):
    init_store(db_path)
    win = reserve_attempt("local", "cid-E", _req(), db_path=db_path)
    record_event(win.submission_id, "PREFLIGHT_ACK_LIMIT", {"override": True}, db_path=db_path)

    con = duckdb.connect(str(db_path))
    rows = con.execute(
        "SELECT kind FROM orders_events WHERE submission_id = ?",
        [win.submission_id],
    ).fetchall()
    con.close()
    assert rows == [("PREFLIGHT_ACK_LIMIT",)]


from xenon.execution.orders_store import working_reservations_for


def test_working_reservations_sums_active_sell_rows(db_path):
    init_store(db_path)
    reserve_attempt("local", "cid-W1", _req(quantity=100), db_path=db_path)
    r2 = reserve_attempt("local", "cid-W2", _req(quantity=50), db_path=db_path)
    mark_submitted(
        submission_id=r2.submission_id,
        ib_order_id="6001",
        perm_id="8001",
        placing_client_id=26,
        db_path=db_path,
    )
    r3 = reserve_attempt("local", "cid-W3", _req(quantity=77), db_path=db_path)
    mark_terminal(
        submission_id=r3.submission_id,
        state="CANCELLED",
        reason_code=None,
        filled_qty=0,
        avg_fill_price=None,
        db_path=db_path,
    )
    res = working_reservations_for("local", "SPY", db_path=db_path)
    assert res.stock_sell_qty == 150
    assert res.short_call_qty == 0
    assert res.long_call_close_qty_same_exp == 0


def test_working_reservations_counts_short_call_only_when_sell_call(db_path):
    init_store(db_path)
    reserve_attempt(
        "local", "cid-C1",
        _req(
            security_type="OPT", action="SELL", right="C",
            expiry="20260620", strike=Decimal("500"), quantity=3,
        ),
        db_path=db_path,
    )
    res = working_reservations_for("local", "SPY", db_path=db_path)
    assert res.short_call_qty == 3
    assert res.stock_sell_qty == 0
