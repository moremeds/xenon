"""compute_health() reports counts and market window. Spec §12.2."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

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


def test_health_uses_position_rule_heartbeat_for_daemon_liveness(engine, monkeypatch):
    """Regression: a healthy daemon can tick without causing a state
    transition. Health must use an explicit heartbeat, not only the latest
    cas_transition event, or it flips red during quiet market periods."""
    from xenon.api.services import position_rules_health as health_mod

    scope = AccountScope(broker="IB", account_env="paper", broker_account="DUHEARTBEAT")
    now = datetime.now(timezone.utc)
    heartbeat_at = now + timedelta(hours=1)
    monkeypatch.setattr(health_mod, "_market_window", lambda now=None: ("open", heartbeat_at + timedelta(hours=1)))

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM events.outbox
                WHERE payload->'scope'->>'broker_account' = :broker_account
                   OR payload->>'broker_account' = :broker_account
                """
            ),
            {"broker_account": scope.broker_account},
        )
        conn.execute(
            text(
                """
                INSERT INTO events.outbox(channel, source, payload, emitted_at)
                VALUES ('position_rule.heartbeat', 'position_rules_handler', CAST(:payload AS jsonb), :emitted_at)
                """
            ),
            {
                "payload": json.dumps({
                    "payload_version": 1,
                    "kind": "position_rules_heartbeat",
                    "evaluated": 0,
                    "scope": scope.as_dict(),
                }),
                "emitted_at": heartbeat_at,
            },
        )

    body = compute_health(engine=engine, scope=scope)

    assert body["daemon_alive"] is True
    assert datetime.fromisoformat(body["last_tick_at"]) == heartbeat_at
