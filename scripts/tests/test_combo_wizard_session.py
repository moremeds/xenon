from __future__ import annotations

import asyncio
import os
from pathlib import Path

import duckdb
import pytest

from xenon.execution import orders_store
from xenon.execution.combo_wizard import session


@pytest.fixture(autouse=True)
def _force_test_mode_on(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    yield


def _db_path() -> Path:
    return Path(os.environ["XENON_ORDERS_DB_PATH"])


def _plan_payload() -> dict:
    return {
        "symbol": "AAPL",
        "type": "combo",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": "2.50",
        "legs": [
            {
                "conId": 1001,
                "action": "BUY",
                "ratio": 1,
                "exchange": "SMART",
            },
            {
                "conId": 1002,
                "action": "SELL",
                "ratio": 1,
                "exchange": "SMART",
            },
        ],
    }


def test_submit_combo_persists_wizard_attempt_and_client_attempt_id(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db_path))
    orders_store.init_store(db_path)

    planned = session.create_session(
        ticker="AAPL",
        intent="OPEN",
        structure_name="Bull Call Spread",
        payload=_plan_payload(),
    )

    result = asyncio.run(
        session.submit_combo(
            planned["session_id"],
            {"target_price": "2.45", "price_basis": "MID"},
        )
    )

    assert result["submission_id"]

    con = duckdb.connect(str(_db_path()))
    try:
        row = con.execute(
            """
            SELECT client_attempt_id, ib_order_id
              FROM wizard_combo_attempts
             WHERE session_id = ?
            """,
            [planned["session_id"]],
        ).fetchone()
    finally:
        con.close()

    assert row is not None
    assert row[0].startswith(f"wiz:{planned['session_id']}:combo:")
    assert row[1] == str(result["orderId"])
