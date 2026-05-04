"""Migration smoke + index plan. Spec §13.3."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine


def test_migration_seed_rows_present():
    engine = get_sync_engine()
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM xenon.bracket_policies")).scalar_one()
    assert count >= 8


def test_partial_unique_index_used_for_active_lookup():
    """EXPLAIN must show the hot-path partial index when seqscan is disabled."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = conn.execute(
            text(
                """
                EXPLAIN
                SELECT * FROM xenon.position_protection
                WHERE state IN ('PENDING_ARM','ARMED')
                  AND broker = 'IB'
                  AND account_env = 'paper'
                  AND broker_account = 'DU1234567'
                """
            )
        ).all()
    plan_text = "\n".join(row[0] for row in plan)
    assert "Index Scan" in plan_text
    assert (
        "ix_position_protection_hot" in plan_text
        or "uq_position_protection_active" in plan_text
    )


def test_check_constraints_reject_invalid_state():
    engine = get_sync_engine()
    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO xenon.position_protection
                      (broker, account_env, broker_account, position_key, position_descriptor,
                       asset_class, rule_kind, state, config)
                    VALUES
                      ('IB', 'paper', 'DU1234567', 'TEST::BOGUS', '{}', 'stock', 'stop_loss',
                       'WHATEVER', '{"threshold_pct": -0.08, "anchor": "entry_price"}')
                    """
                )
            )
