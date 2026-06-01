"""A failure mid-batch leaves partial state that the next run cleans up.

No single wrapping transaction — each ON CONFLICT DO NOTHING runs in its own
commit, so partial writes are durable + idempotent on retry.
"""

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from xenon.api.services.futu_history import futu_history_sync
from xenon.db.queries.futu_history import fetch_fills
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_crash_after_fills_resumes_cleanly(clean_scope, monkeypatch):
    trade_ctx = MagicMock()
    trade_ctx.history_deal_list_query.return_value = (
        0,
        pd.DataFrame(
            [
                {
                    "deal_id": "d1",
                    "order_id": "o",
                    "code": "US.AAPL",
                    "trd_side": "BUY",
                    "qty": 1,
                    "price": 1.0,
                    "create_time": "2024-05-01 10:00:00",
                    "currency": "USD",
                }
            ]
        ),
    )
    # Cash flow step raises mid-run on first invocation.
    trade_ctx.get_acc_cash_flow.side_effect = [
        RuntimeError("simulated OpenD disconnect"),
        (0, pd.DataFrame([])),
    ]
    trade_ctx.close.return_value = None

    quote_ctx = MagicMock()
    quote_ctx.get_rehab.return_value = (0, pd.DataFrame())
    quote_ctx.close.return_value = None

    monkeypatch.setattr(
        "xenon.api.services.futu_history._build_trade_ctx",
        lambda: trade_ctx,
    )
    monkeypatch.setattr(
        "xenon.api.services.futu_history._build_quote_ctx",
        lambda: quote_ctx,
    )
    monkeypatch.setattr(
        "xenon.api.services.futu_history.backfill_fx_series",
        lambda currency, start, end: 0,
    )

    with pytest.raises(RuntimeError):
        futu_history_sync(
            scope=SCOPE,
            since=date(2024, 5, 1),
            until_date=date(2024, 5, 1),
        )
    # The fill from step 2 should be durable despite the step-3 crash.
    assert len(list(fetch_fills(SCOPE, date(2024, 5, 1), date(2024, 5, 1)))) == 1

    # Second run finishes cleanly without duplicating the fill.
    r = futu_history_sync(
        scope=SCOPE,
        since=date(2024, 5, 1),
        until_date=date(2024, 5, 1),
    )
    assert r["fills_added"] == 0
