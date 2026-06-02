from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

# Phase 3: opt out of Phase 2 BoundEngine binding — this file owns its
# own TRUNCATE-based teardown that needs committed cross-test state.
pytestmark = pytest.mark.committed_db
from sqlalchemy import create_engine, text

from xenon.execution import orders_store
from xenon.execution.combo_wizard import session

# --------------------------------------------------------------------------
# Postgres helpers
# --------------------------------------------------------------------------

from xenon._test_db import sync_test_db_url as _sync_url  # worker-aware URL resolver


def _pg_engine():
    return create_engine(_sync_url(), pool_pre_ping=True)


def _cleanup(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.wizard_events CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_combo_attempts CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_sessions CASCADE"))


@pytest.fixture(autouse=True)
def _setup_pg(monkeypatch):
    """Point get_sync_engine() at the test database and clean tables."""
    monkeypatch.setenv("DATABASE_URL", _sync_url())
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
        "symbol": "SPY",
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
                "right": "C",
                "expiry": "20260620",
                "strike": "190",
            },
            {
                "conId": 1002,
                "action": "SELL",
                "ratio": 1,
                "exchange": "SMART",
                "right": "C",
                "expiry": "20260620",
                "strike": "200",
            },
        ],
    }


def test_submit_combo_persists_wizard_attempt_and_client_attempt_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))

    planned = session.create_session(
        ticker="SPY",
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


def test_get_session_rejects_explicit_scope_mismatch():
    sid = "wiz-live-owned"
    engine = _pg_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.wizard_sessions
                    (session_id, ticker, state, structure_name, intent, payload,
                     broker, account_env, broker_account)
                VALUES
                    (:sid, 'AAPL', 'planned', 'Bull Call Spread', 'OPEN', '{}'::jsonb,
                     'IB', 'live', 'U1234567')
                """
            ),
            {"sid": sid},
        )
    engine.dispose()

    with pytest.raises(ValueError, match="scope mismatch"):
        session.get_session(sid)
