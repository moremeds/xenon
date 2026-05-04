"""Concurrent close-claim contention. Spec §5.6, codex N-C1, N-C2."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_close_claims import (
    find_by_order_ref,
    mark_submitted,
    mark_terminal,
    try_claim,
)


@pytest.fixture
def engine():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))


def _claim(engine, position_key, protection_id=1, claim_kind="synthetic_close"):
    return try_claim(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key=position_key,
        claimed_by_protection_id=protection_id,
        claim_kind=claim_kind,
    )


def test_first_try_claim_succeeds(engine):
    claim_id = _claim(engine, "TEST::CC1", protection_id=1001)
    assert claim_id is not None
    found = find_by_order_ref(engine, order_ref=f"xenon-pr-{claim_id}")
    assert found["status"] == "PENDING"
    assert found["claimed_by_protection_id"] == 1001


def test_second_try_claim_returns_none_while_inflight(engine):
    """N-C1: two claims for the same position: only first wins."""
    first = _claim(engine, "TEST::CC2", protection_id=1)
    second = _claim(engine, "TEST::CC2", protection_id=2, claim_kind="native_reconcile_close")
    assert first is not None
    assert second is None


def test_terminal_claim_allows_new_claim(engine):
    claim_id = _claim(engine, "TEST::CC3", protection_id=1)
    mark_terminal(engine, claim_id=claim_id, status="FILLED")

    new_claim = _claim(engine, "TEST::CC3", protection_id=2)
    assert new_claim is not None
    assert new_claim != claim_id


def test_three_way_race_only_one_winner(engine):
    """N-C2 + N-C1: synthetic + synthetic + native reconcile, same position."""
    claim_a = _claim(engine, "TEST::CC4", protection_id=1)
    claim_b = _claim(engine, "TEST::CC4", protection_id=2)
    claim_c = _claim(engine, "TEST::CC4", protection_id=3, claim_kind="native_reconcile_close")
    assert sum(1 for claim_id in (claim_a, claim_b, claim_c) if claim_id is not None) == 1


def test_mark_submitted_tracks_perm_id(engine):
    claim_id = _claim(engine, "TEST::CC5", protection_id=1)
    mark_submitted(engine, claim_id=claim_id, broker_perm_id=99999)
    found = find_by_order_ref(engine, order_ref=f"xenon-pr-{claim_id}")
    assert found["status"] == "SUBMITTED"
    assert found["broker_perm_id"] == 99999
