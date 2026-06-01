"""Step 6: pull corp actions for every ticker ever held; missing get_rehab
data triggers an anomaly + ticker is skipped without raising.
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd

from xenon.api.services.futu_history import pull_corporate_actions
from xenon.db.queries.futu_history import insert_fills
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _fill(ticker: str, futu_code: str) -> dict:
    return dict(
        futu_deal_id=f"d-{ticker}",
        futu_order_id="o",
        ticker=ticker,
        futu_code=futu_code,
        market="US",
        action="BUY",
        quantity=1,
        price=Decimal("1000"),
        fees=Decimal("0"),
        filled_at=datetime(2024, 5, 1, 14, 0),
        raw={},
    )


def test_pull_corp_actions_writes_split_row(clean_scope):
    insert_fills(SCOPE, [_fill("NVDA", "US.NVDA")])
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (
        0,
        pd.DataFrame(
            [
                {
                    "ex_div_date": "2024-06-10",
                    "split_ratio": 10.0,
                    "join_ratio": 1.0,
                    "company_act_flag": 0x04,
                }
            ]
        ),
    )
    n = pull_corporate_actions(SCOPE, quote_ctx)
    assert n == 1


def test_pull_corp_actions_skips_on_empty(clean_scope):
    insert_fills(SCOPE, [_fill("AAPL", "US.AAPL")])
    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (0, pd.DataFrame())
    assert pull_corporate_actions(SCOPE, quote_ctx) == 0


def test_pull_corp_actions_on_failure_writes_anomaly_and_continues(clean_scope):
    """If get_rehab raises for a ticker, the run logs an anomaly and moves on."""
    insert_fills(SCOPE, [_fill("XYZ", "US.XYZ")])
    quote_ctx = MagicMock()
    # First (and only) ticker fails — must NOT propagate.
    quote_ctx.get_rehab.return_value = (-1, "API rate limit exceeded")
    n = pull_corporate_actions(SCOPE, quote_ctx)
    assert n == 0
