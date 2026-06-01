"""Step 5: snapshot_account_state writes the full fetch_portfolio() envelope
to account_snapshots.payload + mirrors net_liquidation into typed columns.
"""

from unittest.mock import MagicMock

import pandas as pd
from sqlalchemy import text

from xenon.api.services.futu_history import snapshot_account_state
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_snapshot_writes_payload_and_typed_columns(clean_scope):
    trade_ctx = MagicMock()
    # C26: Futu SDK returns `total_assets`, not `net_liquidation`.
    trade_ctx.accinfo_query.return_value = (
        0,
        pd.DataFrame([{"total_assets": 12345.67, "cash": 1000, "unrealized_pl": 200}]),
    )
    trade_ctx.position_list_query.return_value = (
        0,
        pd.DataFrame(
            [
                {
                    "code": "US.AAPL",
                    "qty": 10,
                    "cost_price": 150.0,
                    "nominal_price": 160.0,
                    "market_val": 1600.0,
                }
            ]
        ),
    )
    snapshot_account_state(SCOPE, trade_ctx)
    with get_sync_engine().connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT net_liquidation, bankroll, account, payload
                  FROM xenon.account_snapshots
                 WHERE broker='FUTU' AND account_env='paper'
                   AND broker_account='pytest'
                 ORDER BY id DESC LIMIT 1
                """
            )
        ).first()
    # The xenon column net_liquidation = Futu's total_assets (canonical
    # cross-broker name).
    assert float(row.net_liquidation) == 12345.67
    assert float(row.bankroll) == 12345.67  # mirrored
    assert row.account == "pytest"  # C3: account from broker_account
    payload = row.payload
    assert payload["account_summary"]["total_assets"] == 12345.67
    assert payload["positions"][0]["code"] == "US.AAPL"
    assert payload["positions"][0]["market_value"] == 1600.0


def test_snapshot_raises_on_missing_total_assets(clean_scope):
    """C26 guard: if SDK ever stops emitting total_assets, fail loudly."""
    trade_ctx = MagicMock()
    # Pre-C26 mock with 'net_liquidation' but no 'total_assets' — must fail.
    trade_ctx.accinfo_query.return_value = (
        0,
        pd.DataFrame([{"net_liquidation": 100.0, "cash": 0}]),
    )
    trade_ctx.position_list_query.return_value = (0, pd.DataFrame())
    import pytest

    with pytest.raises(RuntimeError, match="total_assets"):
        snapshot_account_state(SCOPE, trade_ctx)
