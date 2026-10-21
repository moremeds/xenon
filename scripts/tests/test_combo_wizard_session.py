from __future__ import annotations

import asyncio

import pytest

from xenon._test_db import committed_pass_stamp

# Phase 3: opt out of Phase 2 BoundEngine binding — this file owns its
# own TRUNCATE-based teardown that needs committed cross-test state.
pytestmark = pytest.mark.committed_db
from sqlalchemy import create_engine, text

from xenon.execution.combo_wizard import session

# --------------------------------------------------------------------------
# Postgres helpers
# --------------------------------------------------------------------------

from xenon._test_db import sync_test_db_url as _sync_url  # worker-aware URL resolver
from xenon.api.services import order_entry


def _pg_engine():
    return create_engine(_sync_url(), pool_pre_ping=True)


def _cleanup(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.wizard_events CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_combo_attempts CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_sessions CASCADE"))
        conn.execute(text("TRUNCATE xenon.account_snapshots CASCADE"))


def _seed_gate4_snapshot(engine):
    """`submit_combo` drives the real /orders/place Gate-4 path — seed the
    observation-stamped snapshot (100 SPY shares covers the spread's SELL
    leg) that ib_sync would have written for the harness scope."""
    import json as _json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    payload = {
        "positions": [
            {
                "ticker": "SPY",
                "structure_type": "Stock",
                "direction": "LONG",
                "contracts": 100,
                "expiry": None,
                "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100, "strike": 0.0, "account": "DU0000000"}],
            }
        ],
        "available_funds": 0,
        "observation": {
            "as_of": now.isoformat(),
            "complete": True,
            "account": "DU0000000",
            "reads": {"positions": True, "open_orders": True, "executions": True},
            # xid8 ordering key + cluster id from a committed txn, as ib_sync stamps them.
            **committed_pass_stamp(),
        },
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.account_snapshots
                  (account, bankroll, payload, snapshot_at, broker, account_env, broker_account)
                VALUES ('DU0000000', 0, CAST(:p AS jsonb), :at, 'IB', 'paper', 'DU0000000')
                """
            ),
            {"p": _json.dumps(payload), "at": now},
        )


@pytest.fixture(autouse=True)
def _setup_pg(monkeypatch):
    """Point get_sync_engine() at the test database and clean tables."""
    monkeypatch.setenv("DATABASE_URL", _sync_url())
    import xenon.db.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_sync_engine", None)

    engine = _pg_engine()
    _cleanup(engine)
    _seed_gate4_snapshot(engine)
    engine.dispose()
    yield
    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _force_test_mode_on(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")

    async def _ib_qualified(body):
        # Stand-in for reqContractDetails (standard SPY options).
        return [
            {"con_id": 7000 + i, "tc": "SPY", "mult": 100, "und_sec": "STK"}
            for i, _ in enumerate(order_entry._gate4_leg_specs(body))
        ]

    monkeypatch.setattr(order_entry, "_resolve_gate4_leg_meta", _ib_qualified)
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


def _place_returns(monkeypatch, status: int, content: dict):
    from fastapi.responses import JSONResponse


    async def _place(_payload):
        return JSONResponse(status_code=status, content=content)

    monkeypatch.setattr(order_entry, "place", _place)


def _session_claim(session_id: str):
    engine = _pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state, current_attempt_id FROM xenon.wizard_sessions WHERE session_id = :sid"),
            {"sid": session_id},
        ).fetchone()
    engine.dispose()
    return row


def test_submit_combo_keeps_claim_on_uncertain_place(monkeypatch):
    """PR-07: IB may hold the combo — the claim stays, so a second submit
    cannot place a duplicate."""
    planned = session.create_session(
        ticker="SPY", intent="OPEN", structure_name="Bull Call Spread", payload=_plan_payload()
    )
    _place_returns(monkeypatch, 502, {"reason_code": "PLACE_OUTCOME_UNKNOWN", "detail": "Do not resubmit."})

    result = asyncio.run(session.submit_combo(planned["session_id"], {"target_price": "2.45"}))

    assert result.status_code == 502
    state, attempt = _session_claim(planned["session_id"])
    assert attempt is not None and str(state).lower() == "submitting"
    with pytest.raises(ValueError):
        asyncio.run(session.submit_combo(planned["session_id"], {"target_price": "2.45"}))


def test_submit_combo_releases_claim_on_rejection(monkeypatch):
    planned = session.create_session(
        ticker="SPY", intent="OPEN", structure_name="Bull Call Spread", payload=_plan_payload()
    )
    _place_returns(monkeypatch, 502, {"reason_code": "IB_REJECT", "detail": "rejected"})

    asyncio.run(session.submit_combo(planned["session_id"], {"target_price": "2.45"}))

    _state, attempt = _session_claim(planned["session_id"])
    assert attempt is None
