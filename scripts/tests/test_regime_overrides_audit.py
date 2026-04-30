"""regime_overrides FK is DEFERRABLE INITIALLY DEFERRED.

The audit row must write in the same transaction as the order_submissions
reservation. With a deferred FK, the INSERT succeeds even when the parent
row does not yet exist; integrity is only checked at COMMIT. This lets the
order route open one transaction, write both rows, and have the broker
submission either succeed (commit both) or fail (roll back both).
"""

from __future__ import annotations

import datetime as dt
import secrets

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import order_submissions, regime_overrides


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture
def submission_row(engine):
    """Reserve a real order_submissions row and yield its submission_id.
    Cleaned up by the autouse truncate fixture in conftest."""
    sub_id = f"SUB-TEST-{secrets.token_hex(6)}"
    now = dt.datetime.now(dt.timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            sa.insert(order_submissions).values(
                submission_id=sub_id,
                user_id="u1",
                ticker="AAPL",
                security_type="STK",
                action="BUY",
                quantity=1,
                state="RESERVED",
                submitted_at=now,
                broker="IB",
                account_env="paper",
                broker_account="DU000",
            )
        )
    return sub_id


def _override_values(submission_id: str) -> dict:
    return dict(
        user_id="u1",
        account_env="paper",
        broker="ib",
        broker_account="DU000",
        submission_id=submission_id,
        route="POST /orders/place",
        binding_side="cri",
        block_reason="CRI CRITICAL — non-hedge entries blocked",
        user_reason="contrarian play, sized small",
        order_payload={"symbol": "AAPL", "quantity": 1},
    )


def test_orphan_submission_id_fails_at_commit_not_insert(engine):
    """FK is DEFERRABLE INITIALLY DEFERRED — INSERT succeeds within the
    transaction; the IntegrityError fires at COMMIT.

    We assert this by opening a connection, INSERTing, asserting the row is
    visible inside the txn, then explicitly COMMITting and catching the
    error there. (Using `with engine.begin()` would conflate the two; we
    want to prove the failure mode is at COMMIT, not at INSERT.)
    """
    with engine.connect() as conn:
        txn = conn.begin()
        # INSERT succeeds because FK check is DEFERRED
        conn.execute(
            sa.insert(regime_overrides).values(
                **_override_values("SUB-DOES-NOT-EXIST"),
            )
        )
        # Row IS visible inside the open transaction
        row_in_txn = conn.execute(
            sa.select(regime_overrides).where(regime_overrides.c.submission_id == "SUB-DOES-NOT-EXIST")
        ).first()
        assert row_in_txn is not None, "INSERT should succeed pre-COMMIT with deferred FK"

        # COMMIT triggers the FK check, which now fails
        with pytest.raises(sa.exc.IntegrityError):
            txn.commit()


def test_audit_insert_with_existing_submission_succeeds(engine, submission_row):
    """Happy path — write the audit row and commit; FK enforced cleanly."""
    with engine.begin() as conn:
        conn.execute(sa.insert(regime_overrides).values(**_override_values(submission_row)))

    # Read back
    with engine.connect() as conn:
        row = conn.execute(sa.select(regime_overrides).where(regime_overrides.c.submission_id == submission_row)).one()
    assert row.binding_side == "cri"
    assert row.block_reason.startswith("CRI CRITICAL")
    assert row.perm_id is None  # filled later by post-fill UPDATE
    assert row.ib_order_id is None


def test_audit_row_and_parent_row_in_same_txn(engine):
    """Same-transaction write of order_submissions + regime_overrides — the
    deferred FK is the whole point of this design."""
    sub_id = f"SUB-TEST-{secrets.token_hex(6)}"
    now = dt.datetime.now(dt.timezone.utc)

    with engine.begin() as conn:
        # Write the audit row FIRST (parent doesn't exist yet — FK deferred)
        conn.execute(sa.insert(regime_overrides).values(**_override_values(sub_id)))
        # Then write the parent
        conn.execute(
            sa.insert(order_submissions).values(
                submission_id=sub_id,
                user_id="u1",
                ticker="AAPL",
                security_type="STK",
                action="BUY",
                quantity=1,
                state="RESERVED",
                submitted_at=now,
                broker="IB",
                account_env="paper",
                broker_account="DU000",
            )
        )
        # Both rows commit together — FK check passes at commit time.

    with engine.connect() as conn:
        audit = conn.execute(sa.select(regime_overrides).where(regime_overrides.c.submission_id == sub_id)).one()
        parent = conn.execute(sa.select(order_submissions).where(order_submissions.c.submission_id == sub_id)).one()
    assert audit.submission_id == parent.submission_id
