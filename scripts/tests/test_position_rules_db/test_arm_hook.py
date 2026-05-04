"""Arm-hook outbox consumer. Spec §6.1, §6.3."""
from __future__ import annotations

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
        conn.execute(text("DELETE FROM xenon.order_fills WHERE exec_id LIKE 'TEST-%'"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'STK::%'"))
        conn.execute(text("DELETE FROM xenon.order_fills WHERE exec_id LIKE 'TEST-%'"))


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
