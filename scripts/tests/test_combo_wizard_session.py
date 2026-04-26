from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from xenon.execution import orders_store
from xenon.execution.combo_wizard import session

# --------------------------------------------------------------------------
# Postgres helpers
# --------------------------------------------------------------------------

_TEST_DB_URL = os.environ.get(
    "DATABASE_URL_TEST",
    "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
)
_SYNC_URL = _TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _pg_engine():
    return create_engine(_SYNC_URL, pool_pre_ping=True)


def _cleanup(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.wizard_events CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_combo_attempts CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_sessions CASCADE"))


@pytest.fixture(autouse=True)
def _setup_pg(monkeypatch):
    """Point get_sync_engine() at the test database and clean tables."""
    monkeypatch.setenv("DATABASE_URL", _SYNC_URL)
    import xenon.db.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_sync_engine", None)

    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()
    yield
    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _force_test_mode_on(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    yield


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
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))

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

    engine = _pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT combo_contract, ib_order_id
                  FROM xenon.wizard_combo_attempts
                 WHERE session_id = :sid
                """
            ),
            {"sid": planned["session_id"]},
        ).fetchone()
    engine.dispose()

    assert row is not None
    # client_attempt_id is stored inside the combo_contract JSONB
    client_attempt_id = row[0].get("client_attempt_id") if isinstance(row[0], dict) else None
    assert client_attempt_id is not None
    assert client_attempt_id.startswith(f"wiz:{planned['session_id']}:combo:")
    assert row[1] == str(result["orderId"])
