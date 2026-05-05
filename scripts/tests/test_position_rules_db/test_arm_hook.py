"""Arm-hook outbox consumer. Spec §6.1, §6.3."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.brackets.arm_hook import on_fill_event


@pytest.fixture
def engine():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'STK::%'"))
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'CS::%'"))
        conn.execute(text("DELETE FROM xenon.order_fills WHERE exec_id LIKE 'TEST-%'"))
        conn.execute(text("DELETE FROM xenon.wizard_combo_attempts WHERE attempt_id LIKE 'TEST-%'"))
        conn.execute(text("DELETE FROM xenon.wizard_sessions WHERE session_id LIKE 'TEST-%'"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'STK::%'"))
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'CS::%'"))
        conn.execute(text("DELETE FROM xenon.order_fills WHERE exec_id LIKE 'TEST-%'"))
        conn.execute(text("DELETE FROM xenon.wizard_combo_attempts WHERE attempt_id LIKE 'TEST-%'"))
        conn.execute(text("DELETE FROM xenon.wizard_sessions WHERE session_id LIKE 'TEST-%'"))


def _fill_event(exec_id, ticker, side, price=100.0, sec_type="STK"):
    return {
        "exec_id": exec_id,
        "submission_id": None,
        "combo_attempt_id": None,
        "perm_id": "1",
        "ticker": ticker,
        "side": side,
        "qty": 1,
        "price": str(price),
        "filled_at": "2026-05-04T14:23:11+00:00",
        "metadata": {"sec_type": sec_type, "legacy_source": "position_rules_test"},
        "broker": "IB",
        "account_env": "paper",
        "broker_account": "DU1234567",
        "con_id": 12345,
    }


def _persist_fill(engine, event):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.order_fills
                    (exec_id, submission_id, combo_attempt_id, perm_id, con_id, ticker,
                     side, qty, price, filled_at, metadata, broker, account_env, broker_account)
                VALUES
                    (:exec_id, :submission_id, :combo_attempt_id, :perm_id, :con_id, :ticker,
                     :side, :qty, :price, :filled_at, CAST(:metadata AS jsonb), :broker,
                     :account_env, :broker_account)
                ON CONFLICT (exec_id) DO NOTHING
                """
            ),
            {
                **event,
                "filled_at": datetime.fromisoformat(event["filled_at"]),
                "metadata": '{"sec_type": "STK", "legacy_source": "position_rules_test"}',
            },
        )


def test_single_leg_stock_fill_arms_two_rules(engine):
    event = _fill_event("TEST-EX-1", "AAPL", "BUY")
    _persist_fill(engine, event)

    on_fill_event(engine, event)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT rule_kind FROM xenon.position_protection
                WHERE position_key = 'STK::AAPL' AND state = 'PENDING_ARM'
                ORDER BY rule_kind
                """
            )
        ).all()
    assert [row.rule_kind for row in rows] == ["stop_loss", "trailing_tp"]


def test_replay_is_idempotent(engine):
    """Consumer replays fill.recorded: no duplicate rows."""
    event = _fill_event("TEST-EX-2", "MSFT", "BUY")
    _persist_fill(engine, event)

    on_fill_event(engine, event)
    on_fill_event(engine, event)

    with engine.connect() as conn:
        count = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM xenon.position_protection
                WHERE position_key = 'STK::MSFT' AND state = 'PENDING_ARM'
                """
            )
        ).scalar_one()
    assert count == 2


def test_combo_credit_spread_descriptor_has_credit_metadata(engine):
    attempt_id = "TEST-CS-ATTEMPT"
    session_id = "TEST-CS-SESSION"
    legs = [
        {
            "secType": "OPT",
            "symbol": "SPY",
            "expiry": "20260516",
            "strike": 580.0,
            "right": "P",
            "action": "SELL",
            "ratio": 1,
            "fill_price": 1.40,
            "conId": 58001,
        },
        {
            "secType": "OPT",
            "symbol": "SPY",
            "expiry": "20260516",
            "strike": 575.0,
            "right": "P",
            "action": "BUY",
            "ratio": 1,
            "fill_price": 0.40,
            "conId": 57501,
        },
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.wizard_sessions
                    (session_id, ticker, state, structure_name, created_at, updated_at,
                     broker, account_env, broker_account)
                VALUES
                    (:session_id, 'SPY', 'working', 'credit_spread', NOW(), NOW(),
                     'IB', 'paper', 'DU1234567')
                """
            ),
            {"session_id": session_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO xenon.wizard_combo_attempts
                    (attempt_id, session_id, ticker, structure_name, legs, state,
                     submitted_at, updated_at, broker, account_env, broker_account)
                VALUES
                    (:attempt_id, :session_id, 'SPY', 'credit_spread', CAST(:legs AS jsonb),
                     'FILLED', NOW(), NOW(), 'IB', 'paper', 'DU1234567')
                """
            ),
            {"attempt_id": attempt_id, "session_id": session_id, "legs": json.dumps(legs)},
        )

    first = _fill_event("TEST-CS-FILL-1", "SPY", "BUY", price=-1.0, sec_type="BAG")
    first["combo_attempt_id"] = attempt_id
    second = _fill_event("TEST-CS-FILL-2", "SPY", "BUY", price=-1.0, sec_type="BAG")
    second["combo_attempt_id"] = attempt_id
    _persist_fill(engine, first)
    _persist_fill(engine, second)

    on_fill_event(engine, first)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT rule_kind, position_descriptor
                FROM xenon.position_protection
                WHERE position_key = 'CS::SPY::20260516::580::575::P'
                ORDER BY rule_kind
                """
            )
        ).all()

    assert [row.rule_kind for row in rows] == ["stop_loss", "take_profit_fixed"]
    descriptor = rows[0].position_descriptor
    assert descriptor["credit_received"] == 1.0
    assert descriptor["short_strike"] == 580.0
    assert descriptor["short_right"] == "P"
    assert descriptor["legs"][0]["con_id"] == 58001
    assert descriptor["legs"][0]["sec_type"] == "OPT"


def test_combo_with_incomplete_manifest_leg_is_not_armed(engine):
    attempt_id = "TEST-BAD-ATTEMPT"
    session_id = "TEST-BAD-SESSION"
    legs = [
        {
            "secType": "OPT",
            "symbol": "SPY",
            "strike": 580.0,
            "right": "P",
            "action": "SELL",
            "ratio": 1,
            "fill_price": 1.40,
            "conId": 58001,
        },
        {
            "secType": "OPT",
            "symbol": "SPY",
            "expiry": "20260516",
            "strike": 575.0,
            "right": "P",
            "action": "BUY",
            "ratio": 1,
            "fill_price": 0.40,
            "conId": 57501,
        },
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.wizard_sessions
                    (session_id, ticker, state, structure_name, created_at, updated_at,
                     broker, account_env, broker_account)
                VALUES
                    (:session_id, 'SPY', 'working', 'credit_spread', NOW(), NOW(),
                     'IB', 'paper', 'DU1234567')
                """
            ),
            {"session_id": session_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO xenon.wizard_combo_attempts
                    (attempt_id, session_id, ticker, structure_name, legs, state,
                     submitted_at, updated_at, broker, account_env, broker_account)
                VALUES
                    (:attempt_id, :session_id, 'SPY', 'credit_spread', CAST(:legs AS jsonb),
                     'FILLED', NOW(), NOW(), 'IB', 'paper', 'DU1234567')
                """
            ),
            {"attempt_id": attempt_id, "session_id": session_id, "legs": json.dumps(legs)},
        )

    first = _fill_event("TEST-BAD-FILL-1", "SPY", "BUY", price=-1.0, sec_type="BAG")
    first["combo_attempt_id"] = attempt_id
    second = _fill_event("TEST-BAD-FILL-2", "SPY", "BUY", price=-1.0, sec_type="BAG")
    second["combo_attempt_id"] = attempt_id
    _persist_fill(engine, first)
    _persist_fill(engine, second)

    on_fill_event(engine, first)

    with engine.connect() as conn:
        protection_count = conn.execute(
            text("SELECT COUNT(*) FROM xenon.position_protection WHERE position_key LIKE 'CS::SPY::%'")
        ).scalar_one()
        unsupported = conn.execute(
            text(
                """
                SELECT payload FROM events.outbox
                WHERE source = 'arm_hook_unsupported'
                  AND payload->>'exec_id' = 'TEST-BAD-FILL-1'
                ORDER BY id DESC
                LIMIT 1
                """
            )
        ).one()

    assert protection_count == 0
    assert unsupported.payload["reason"] == "combo_leg_missing_expiry"
