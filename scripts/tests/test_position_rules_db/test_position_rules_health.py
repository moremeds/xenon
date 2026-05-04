"""compute_health() reports counts and market window. Spec §12.2."""
from __future__ import annotations

import pytest

from xenon.api.services.position_rules_health import compute_health
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope


@pytest.fixture
def engine():
    return get_sync_engine()


def _scope() -> AccountScope:
    return AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")


def test_health_reports_market_window(engine):
    body = compute_health(engine=engine, scope=_scope())
    assert body["schema_version"] == 1
    assert body["market_window"] in ("open", "closed", "pre_open", "post_close")
    assert "rule_counts_by_state" in body
    assert "claim_counts_by_status" in body
    assert "ib_connected" in body


def test_health_groups_state_counts(engine):
    body = compute_health(engine=engine, scope=_scope())
    counts = body["rule_counts_by_state"]
    for state in ("PENDING_ARM", "ARMED", "TRIGGERED", "FAILED", "CANCELED", "CLOSED", "SUPERSEDED"):
        assert state in counts
        assert isinstance(counts[state], int)
