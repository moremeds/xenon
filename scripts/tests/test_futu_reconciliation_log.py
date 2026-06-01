"""Reconciliation log: $25 floor or 0.1% of NAV, whichever larger.

A $50 delta on a $10,050 NAV is 0.5% — exceeds the 0.1% threshold ($10.05),
so `exceeded` MUST be True.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.futu_history import write_recon_daily
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_threshold_scales_with_nav(clean_scope):
    write_recon_daily(
        SCOPE,
        date(2024, 5, 1),
        reported=Decimal("10050"),
        reconstructed=Decimal("10000"),
        threshold=max(Decimal("25"), Decimal("10050") * Decimal("0.001")),
    )
    with get_sync_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT delta, threshold, exceeded "
                "FROM xenon.futu_reconciliation_log "
                "WHERE broker='FUTU' AND account_env='paper' "
                "  AND broker_account='pytest' AND kind='daily'"
            )
        ).first()
    assert row.delta == Decimal("50")
    assert row.exceeded is True


def test_below_threshold_not_exceeded(clean_scope):
    """$10 delta on $50000 NAV is below the $50 0.1% threshold."""
    write_recon_daily(
        SCOPE,
        date(2024, 5, 2),
        reported=Decimal("50010"),
        reconstructed=Decimal("50000"),
        threshold=max(Decimal("25"), Decimal("50010") * Decimal("0.001")),
    )
    with get_sync_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT delta, threshold, exceeded "
                "FROM xenon.futu_reconciliation_log "
                "WHERE broker='FUTU' AND account_env='paper' "
                "  AND broker_account='pytest' AND kind='daily' "
                "  AND recon_date='2024-05-02'"
            )
        ).first()
    assert row.delta == Decimal("10")
    assert row.exceeded is False
