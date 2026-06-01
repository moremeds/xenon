"""Running sync twice with the same inputs writes each row exactly once."""

from datetime import date
from unittest.mock import MagicMock

import pandas as pd

from xenon.api.services.futu_history import futu_history_sync
from xenon.db.queries.futu_history import fetch_fills
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _fake_deals_df():
    return (
        0,
        pd.DataFrame(
            [
                {
                    "deal_id": "d1",
                    "order_id": "o1",
                    "code": "US.AAPL",
                    "trd_side": "BUY",
                    "qty": 100,
                    "price": 150.0,
                    "create_time": "2024-05-01 10:00:00",
                    "currency": "USD",
                }
            ]
        ),
    )


def _fake_cash_df():
    return (
        0,
        pd.DataFrame(
            [
                {
                    "cashflow_id": "f1",
                    "cashflow_type": "FUND_IN",
                    "cashflow_amount": 1000.0,
                    "currency": "USD",
                    "cashflow_remark": "wire",
                    "clearing_date": "2024-05-01",
                }
            ]
        ),
    )


def test_double_sync_is_a_noop(clean_scope, monkeypatch):
    trade_ctx = MagicMock()
    trade_ctx.history_deal_list_query.return_value = _fake_deals_df()
    trade_ctx.get_acc_cash_flow.return_value = _fake_cash_df()
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
    # FX backfill is HTTP — neutralize.
    monkeypatch.setattr(
        "xenon.api.services.futu_history.backfill_fx_series",
        lambda currency, start, end: 0,
    )

    r1 = futu_history_sync(
        scope=SCOPE,
        since=date(2024, 5, 1),
        until_date=date(2024, 5, 1),
    )
    r2 = futu_history_sync(
        scope=SCOPE,
        since=date(2024, 5, 1),
        until_date=date(2024, 5, 1),
    )
    assert r1["fills_added"] == 1
    assert r2["fills_added"] == 0
    assert r1["cash_flows_added"] == 1
    assert r2["cash_flows_added"] == 0
    assert len(list(fetch_fills(SCOPE, date(2024, 5, 1), date(2024, 5, 1)))) == 1
