import duckdb

from xenon.execution import orders_store
from xenon.execution.combo_wizard import store


def test_orders_store_init_store_creates_combo_wizard_tables(tmp_path):
    db_path = tmp_path / "orders.duckdb"

    orders_store.init_store(db_path)

    assert {
        "wizard_sessions",
        "wizard_combo_attempts",
        "wizard_session_events",
        "wizard_protection",
    } <= store.list_tables(db_path)

    con = duckdb.connect(str(db_path))
    try:
        session_cols = {
            row[1] for row in con.execute("PRAGMA table_info('wizard_sessions')").fetchall()
        }
    finally:
        con.close()

    assert {
        "session_id",
        "ticker",
        "state",
        "structure_name",
        "intent",
        "payload_json",
        "created_at",
        "updated_at",
    } <= session_cols
