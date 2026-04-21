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
