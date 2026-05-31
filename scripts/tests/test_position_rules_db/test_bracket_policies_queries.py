"""Bracket-policies SQL resolver. Spec §5.2, codex N-S1."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.bracket_policies import resolve_for_scope


@pytest.fixture
def engine_with_account_specific_override():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.bracket_policies
                    (broker, account_env, broker_account, asset_class, rule_kind, auto_place, config)
                VALUES
                    ('IB', NULL, NULL, 'long_option', 'stop_loss', TRUE,
                     '{"threshold_pct": -0.10, "anchor": "entry_price"}'),
                    (NULL, NULL, 'DU1234567', 'long_option', 'stop_loss', TRUE,
                     '{"threshold_pct": -0.20, "anchor": "entry_price"}')
                ON CONFLICT DO NOTHING
                """
            )
        )
    yield engine
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM xenon.bracket_policies
                WHERE (broker = 'IB' AND broker_account IS NULL AND asset_class = 'long_option')
                   OR (broker IS NULL AND broker_account = 'DU1234567' AND asset_class = 'long_option')
                """
            )
        )


def test_account_specific_override_beats_broker_wide(engine_with_account_specific_override):
    rows = resolve_for_scope(
        engine_with_account_specific_override,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        asset_class="long_option",
    )
    assert rows[0].broker_account == "DU1234567"
    assert rows[0].config["threshold_pct"] == -0.20
