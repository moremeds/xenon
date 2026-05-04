"""position_protection CRUD + CAS + partial-unique re-arm. Spec §5.1, §7."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import (
    cas_transition,
    insert_pending_arm,
    list_active_rows,
)


@pytest.fixture
def engine():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))


def _descriptor(symbol="AAPL"):
    return {"asset_class": "stock", "legs": [{"sec_type": "STK", "symbol": symbol}]}


def _insert(engine, position_key):
    return insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key=position_key,
        position_descriptor=_descriptor(),
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )


def test_insert_pending_arm_emits_outbox_row(engine):
    pid = _insert(engine, "TEST::AAPL")
    assert pid is not None

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT payload->>'new_state' AS new_state
                FROM events.outbox
                WHERE channel = 'position_rule.transition'
                  AND payload->>'protection_id' = :pid
                ORDER BY id DESC LIMIT 1
                """
            ),
            {"pid": str(pid)},
        ).first()
    assert row.new_state == "PENDING_ARM"


def test_cas_transition_pending_to_armed_succeeds(engine):
    pid = _insert(engine, "TEST::AAPL2")
    assert cas_transition(
        engine,
        protection_id=pid,
        expected_state="PENDING_ARM",
        new_state="ARMED",
        reason="armed_synthetic",
    )


def test_cas_transition_rejects_stale_expected_state(engine):
    pid = _insert(engine, "TEST::AAPL3")
    assert cas_transition(
        engine,
        protection_id=pid,
        expected_state="PENDING_ARM",
        new_state="ARMED",
        reason="armed",
    )
    assert (
        cas_transition(
            engine,
            protection_id=pid,
            expected_state="PENDING_ARM",
            new_state="TRIGGERED",
            reason="trigger",
        )
        is False
    )


def test_partial_unique_allows_rearm_after_canceled(engine):
    """N-S2 regression: terminal CANCELED row does not block re-arm."""
    pid1 = _insert(engine, "TEST::AAPL_REARM")
    assert cas_transition(
        engine,
        protection_id=pid1,
        expected_state="PENDING_ARM",
        new_state="CANCELED",
        reason="manual_cancel",
    )

    pid2 = _insert(engine, "TEST::AAPL_REARM")
    assert pid2 is not None
    assert pid2 != pid1


def test_list_active_rows_filters_terminal(engine):
    pid = _insert(engine, "TEST::AAPL_LIST")
    assert cas_transition(
        engine,
        protection_id=pid,
        expected_state="PENDING_ARM",
        new_state="CANCELED",
        reason="cancel",
    )

    active = list_active_rows(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
    )
    assert all(row["protection_id"] != pid for row in active)
