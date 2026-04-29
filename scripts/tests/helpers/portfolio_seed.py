"""Test helper: seed xenon.account_snapshots.payload for portfolio_loader tests.

Used by every reader-migration task in
docs/plans/2026-04-27-portfolio-postgres-read-path-phase2.md.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import insert

from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots


def seed_portfolio_snapshot(
    payload: dict[str, Any],
    *,
    broker: str = "IB",
    account_env: str = "paper",
    broker_account: str = "DU0000000",
    bankroll: Decimal | float = 100_000,
    peak_value: Decimal | float = 100_000,
    net_liquidation: Decimal | float = 100_000,
) -> None:
    """Insert one account_snapshots row. Caller controls scope and payload."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account=broker_account,
                bankroll=Decimal(str(bankroll)),
                peak_value=Decimal(str(peak_value)),
                net_liquidation=Decimal(str(net_liquidation)),
                payload=payload,
                broker=broker,
                account_env=account_env,
                broker_account=broker_account,
            )
        )
