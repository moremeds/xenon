import duckdb
import pytest

from xenon.execution import orders_store


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
