"""Tests for FutuHistoryFetcher — US filter, 90-day paging, rate-limit
retry, malformed row tolerance, cash flow path."""

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from xenon.clients.futu_history_fetcher import FutuHistoryFetcher


def _make_deals_page(rows):
    return (0, pd.DataFrame(rows))


def test_fetch_deals_filters_non_us():
    sdk = MagicMock()
    sdk.history_deal_list_query.return_value = _make_deals_page(
        [
            {
                "deal_id": "1",
                "order_id": "10",
                "code": "US.AAPL",
                "trd_side": "BUY",
                "qty": 100,
                "price": 150.0,
                "create_time": "2024-05-01 10:00:00",
                "counter_broker_id": "",
                "currency": "USD",
            },
            {
                "deal_id": "2",
                "order_id": "11",
                "code": "HK.00700",
                "trd_side": "BUY",
                "qty": 100,
                "price": 300.0,
                "create_time": "2024-05-01 10:01:00",
                "counter_broker_id": "",
                "currency": "HKD",
            },
        ]
    )
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    fills = list(f.fetch_deals(date(2024, 5, 1), date(2024, 5, 1), acc_id="acct-1"))
    assert [x.futu_deal_id for x in fills] == ["1"]
    assert fills[0].market == "US"
    assert fills[0].ticker == "AAPL"


def test_fetch_deals_paginates_across_90_day_chunks():
    sdk = MagicMock()
    sdk.history_deal_list_query.return_value = _make_deals_page([])
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    list(f.fetch_deals(date(2024, 1, 1), date(2024, 12, 31), acc_id="acct-1"))
    # 365 / 90 → 5 chunks (90,90,90,90, last partial).
    assert sdk.history_deal_list_query.call_count == 5


def test_fetch_deals_retries_then_raises_on_rate_limit():
    sdk = MagicMock()
    sdk.history_deal_list_query.side_effect = [
        (-1, "API rate limit exceeded"),
        (-1, "API rate limit exceeded"),
        (-1, "API rate limit exceeded"),
    ]
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL", retry_sleep_s=0.0, max_retries=3)
    with pytest.raises(RuntimeError, match="rate limit"):
        list(f.fetch_deals(date(2024, 5, 1), date(2024, 5, 1), acc_id="acct-1"))


def test_fetch_deals_skips_malformed_rows():
    sdk = MagicMock()
    sdk.history_deal_list_query.return_value = _make_deals_page(
        [
            {
                "deal_id": "1",
                "order_id": "10",
                "code": "US.AAPL",
                "trd_side": "BUY",
                "qty": 100,
                "price": 150.0,
                "create_time": "2024-05-01 10:00:00",
                "currency": "USD",
            },
            {"deal_id": None, "code": "US.MSFT"},
        ]
    )
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    fills = list(f.fetch_deals(date(2024, 5, 1), date(2024, 5, 1), acc_id="acct-1"))
    assert len(fills) == 1


def test_fetch_deals_sums_multiple_fee_columns():
    sdk = MagicMock()
    sdk.history_deal_list_query.return_value = _make_deals_page(
        [
            {
                "deal_id": "1",
                "order_id": "10",
                "code": "US.AAPL",
                "trd_side": "BUY",
                "qty": 100,
                "price": 150.0,
                "create_time": "2024-05-01 10:00:00",
                "currency": "USD",
                "commission": "1.50",
                "platform_fee": "0.50",
                "clearing_fee": "0.10",
            },
        ]
    )
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    fills = list(f.fetch_deals(date(2024, 5, 1), date(2024, 5, 1), acc_id="acct-1"))
    assert float(fills[0].fees) == 2.10


def test_fetch_cash_flow_returns_typed_rows():
    sdk = MagicMock()
    sdk.get_acc_cash_flow.return_value = (
        0,
        pd.DataFrame(
            [
                {
                    "flow_id": "f1",
                    "cashflow_type": "FUND_IN",
                    "amount": 1000.0,
                    "currency": "USD",
                    "remark": "wire",
                    "cashflow_remark": "",
                    "create_time": "2024-05-01 09:00:00",
                },
            ]
        ),
    )
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    flows = list(f.fetch_cash_flow(date(2024, 5, 1), date(2024, 5, 1), acc_id="acct-1"))
    assert flows[0].futu_flow_id == "f1"
    assert flows[0].futu_type_raw == "FUND_IN"
    assert float(flows[0].amount_original) == 1000.0
