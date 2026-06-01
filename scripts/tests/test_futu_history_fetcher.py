"""Tests for FutuHistoryFetcher — US filter, 90-day deal paging, day-by-day
cash flow iteration, rate-limit retry, malformed row tolerance.

Column names in the mocks mirror the real Futu SDK (futu==10.03.6308 +
Futu OpenAPI docs verified 2026-06-01):
  - history_deal_list_query rows: deal_id, order_id, code, trd_side, qty,
    price, create_time, currency, counter_broker_id (NO fee columns).
  - get_acc_cash_flow rows: cashflow_id, clearing_date, cashflow_type,
    cashflow_amount, currency, cashflow_remark.
"""

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
            },
            {"deal_id": None, "code": "US.MSFT"},
        ]
    )
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    fills = list(f.fetch_deals(date(2024, 5, 1), date(2024, 5, 1), acc_id="acct-1"))
    assert len(fills) == 1


def test_fetch_deals_fees_always_zero_no_fee_columns_on_deal_rows():
    """history_deal_list_query never returns fee columns. Even if a mock
    accidentally includes one, fees on FutuFill must be Decimal('0')."""
    from decimal import Decimal

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
                # Intentionally include a phantom fee column — must be ignored.
                "commission": "999",
            },
        ]
    )
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    fills = list(f.fetch_deals(date(2024, 5, 1), date(2024, 5, 1), acc_id="acct-1"))
    assert fills[0].fees == Decimal("0")


def test_fetch_cash_flow_calls_per_day():
    """get_acc_cash_flow takes a SINGLE clearing_date — for a 3-day range
    the fetcher must call the SDK three times with distinct dates."""
    sdk = MagicMock()
    sdk.get_acc_cash_flow.return_value = (0, pd.DataFrame())
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    list(f.fetch_cash_flow(date(2024, 5, 1), date(2024, 5, 3), acc_id="acct-1"))
    assert sdk.get_acc_cash_flow.call_count == 3
    seen = {call.kwargs.get("clearing_date") for call in sdk.get_acc_cash_flow.call_args_list}
    assert seen == {"2024-05-01", "2024-05-02", "2024-05-03"}


def test_fetch_cash_flow_returns_typed_rows():
    sdk = MagicMock()
    # Real Futu SDK column names: cashflow_id, cashflow_type,
    # cashflow_amount, clearing_date, currency, cashflow_remark.
    sdk.get_acc_cash_flow.return_value = (
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
                    "settlement_date": "2024-05-02",
                    "cashflow_direction": "IN",
                },
            ]
        ),
    )
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    flows = list(f.fetch_cash_flow(date(2024, 5, 1), date(2024, 5, 1), acc_id="acct-1"))
    assert len(flows) == 1
    cf = flows[0]
    assert cf.futu_flow_id == "f1"
    assert cf.futu_type_raw == "FUND_IN"
    assert float(cf.amount_original) == 1000.0
    assert cf.original_currency == "USD"
    # clearing_date '2024-05-01' → midnight ET datetime
    assert cf.flow_at.year == 2024
    assert cf.flow_at.month == 5
    assert cf.flow_at.day == 1
    assert cf.flow_at.hour == 0
    assert cf.flow_at.tzinfo is not None


def test_fetch_cash_flow_skips_malformed_rows():
    sdk = MagicMock()
    sdk.get_acc_cash_flow.return_value = (
        0,
        pd.DataFrame(
            [
                {"cashflow_id": None, "cashflow_type": "FUND_IN"},
                {
                    "cashflow_id": "f1",
                    "cashflow_type": "FUND_IN",
                    "cashflow_amount": 500.0,
                    "currency": "USD",
                    "clearing_date": "2024-05-01",
                },
            ]
        ),
    )
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    flows = list(f.fetch_cash_flow(date(2024, 5, 1), date(2024, 5, 1), acc_id="acct-1"))
    assert [c.futu_flow_id for c in flows] == ["f1"]


def test_fetch_cash_flow_legacy_mock_columns_still_work():
    """Defensive: if a test ever uses the old (wrong) column names like
    `amount` and `create_time`, the fallback path keeps it working so
    older mocks in the suite don't silently break."""
    sdk = MagicMock()
    sdk.get_acc_cash_flow.return_value = (
        0,
        pd.DataFrame(
            [
                {
                    "flow_id": "f1",
                    "type": "DIVIDEND",
                    "amount": 250.0,
                    "currency": "USD",
                    "remark": "div",
                    "create_time": "2024-05-01 10:00:00",
                },
            ]
        ),
    )
    f = FutuHistoryFetcher(trade_ctx=sdk, trd_env="REAL")
    flows = list(f.fetch_cash_flow(date(2024, 5, 1), date(2024, 5, 1), acc_id="acct-1"))
    assert len(flows) == 1
    assert flows[0].futu_flow_id == "f1"
    assert flows[0].futu_type_raw == "DIVIDEND"
    assert float(flows[0].amount_original) == 250.0
