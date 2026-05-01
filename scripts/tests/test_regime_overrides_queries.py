"""Integration tests for the regime_overrides query module.

Uses DATABASE_URL_TEST. Each test inserts a fresh order_submissions
parent row with a unique submission_id and asserts the override row
links / lists / updates correctly. Cleanup is per-test via savepoint.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import create_async_engine

from xenon.db.queries.regime_overrides import (
    get_override_for_submission,
    insert_override,
    list_overrides,
    mark_broker_ids,
)
from xenon.db.schema import order_submissions, regime_overrides

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine():
    url = os.environ.get("DATABASE_URL_TEST") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL_TEST / DATABASE_URL not set")
    engine = create_async_engine(url, future=True)
    yield engine
    await engine.dispose()


def _scope() -> dict:
    return {
        "broker": "IB",
        "account_env": "paper",
        "broker_account": "DU_TEST_REGIME",
    }


async def _insert_submission(conn, submission_id: str, *, scope: dict) -> None:
    now = datetime.now(timezone.utc)
    await conn.execute(
        insert(order_submissions).values(
            submission_id=submission_id,
            user_id="u_regime_test",
            client_attempt_id=submission_id + "-attempt",
            ticker="AAPL",
            security_type="OPT",
            action="BUY",
            quantity=1,
            state="PENDING",
            submitted_at=now,
            updated_at=now,
            **scope,
        )
    )


async def _cleanup_submission(conn, submission_id: str) -> None:
    await conn.execute(delete(regime_overrides).where(regime_overrides.c.submission_id == submission_id))
    await conn.execute(delete(order_submissions).where(order_submissions.c.submission_id == submission_id))


async def test_insert_override_returns_row_id(engine):
    sub_id = f"reg-test-{uuid.uuid4().hex[:8]}"
    scope = _scope()
    async with engine.begin() as conn:
        await _insert_submission(conn, sub_id, scope=scope)
        row_id = await insert_override(
            conn,
            user_id="u_regime_test",
            **scope,
            submission_id=sub_id,
            client_attempt_id=None,
            route="POST /orders/place",
            vcg_tier="TIER_1",
            cri_tier="NORMAL",
            binding_side="vcg",
            block_reason="TIER_1 — non-hedge entries blocked",
            user_reason="contrarian play with tight stop",
            order_payload={"ticker": "NVDA", "qty": 1},
        )
        assert isinstance(row_id, int) and row_id > 0
        await _cleanup_submission(conn, sub_id)


async def test_insert_override_rejects_scope_mismatch_at_commit(engine):
    """ISSUE-5 regression: composite FK rejects override scope drift."""
    sub_id = f"reg-test-{uuid.uuid4().hex[:8]}"
    scope = _scope()
    drifted_scope = {**scope, "broker_account": "DIFFERENT_ACCOUNT"}
    with pytest.raises(Exception) as exc_info:
        async with engine.begin() as conn:
            await _insert_submission(conn, sub_id, scope=scope)
            await insert_override(
                conn,
                user_id="u_regime_test",
                **drifted_scope,
                submission_id=sub_id,
                client_attempt_id=None,
                route="POST /orders/place",
                vcg_tier="TIER_1",
                cri_tier="NORMAL",
                binding_side="vcg",
                block_reason="TIER_1",
                user_reason="should fail at commit",
                order_payload={},
            )
    assert "fk_regime_overrides_submission_scope" in str(exc_info.value)


async def test_mark_broker_ids_updates_row(engine):
    sub_id = f"reg-test-{uuid.uuid4().hex[:8]}"
    scope = _scope()
    async with engine.begin() as conn:
        await _insert_submission(conn, sub_id, scope=scope)
        await insert_override(
            conn,
            user_id="u_regime_test",
            **scope,
            submission_id=sub_id,
            client_attempt_id=None,
            route="POST /orders/place",
            vcg_tier="TIER_1",
            cri_tier="NORMAL",
            binding_side="vcg",
            block_reason="TIER_1",
            user_reason="testing post-fill update path",
            order_payload={},
        )
        n = await mark_broker_ids(conn, submission_id=sub_id, perm_id=999_888_777, ib_order_id=12345)
        assert n == 1
        row = await get_override_for_submission(conn, submission_id=sub_id)
        assert row is not None
        assert row["perm_id"] == 999_888_777
        assert row["ib_order_id"] == 12345
        await _cleanup_submission(conn, sub_id)


async def test_list_overrides_filters_by_scope_and_orders_newest_first(engine):
    scope_a = _scope()
    scope_b = {**scope_a, "broker_account": "DU_OTHER_TEST"}
    ids = []
    async with engine.begin() as conn:
        for scope in (scope_a, scope_a, scope_b):
            sub_id = f"reg-test-{uuid.uuid4().hex[:8]}"
            ids.append((sub_id, scope))
            await _insert_submission(conn, sub_id, scope=scope)
            await insert_override(
                conn,
                user_id="u_regime_test",
                **scope,
                submission_id=sub_id,
                client_attempt_id=None,
                route="POST /orders/place",
                vcg_tier="TIER_2",
                cri_tier="NORMAL",
                binding_side="vcg",
                block_reason="TIER_2",
                user_reason="list ordering smoke test",
                order_payload={},
            )
        rows = await list_overrides(
            conn,
            account_env=scope_a["account_env"],
            broker_account=scope_a["broker_account"],
            limit=10,
        )
        # 2 rows for scope_a, none for scope_b
        relevant = [r for r in rows if r["submission_id"] in {ids[0][0], ids[1][0]}]
        assert len(relevant) == 2
        # Sorted newest first
        assert relevant[0]["ts"] >= relevant[1]["ts"]
        # scope_b row not present
        assert all(r["submission_id"] != ids[2][0] for r in relevant)
        for sub_id, _ in ids:
            await _cleanup_submission(conn, sub_id)
