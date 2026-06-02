"""FutuClient.fetch_history_deals + fetch_capital_flow — contract tests.

Mocks the Futu SDK's TrdContext (`history_deal_list_query` and
`history_funds_flow_query`) — no live OpenD needed. Verifies:
  - 90-day window pagination (Futu's documented cap per call)
  - Date range plumbing (start/end forwarded as ISO strings to SDK)
  - Row → dict conversion shape (matches what M4 sync writer expects)
  - SDK error response surfaces as FutuConnectionError
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from xenon.clients.futu_client import FutuClient
from xenon.clients.futu_exceptions import FutuError

RET_OK = 0  # mirrors Futu SDK's RET_OK
RET_ERROR = -1


def _client_with_mocked_ctx(
    deal_rows: list[dict] | None = None, flow_rows: list[dict] | None = None
) -> tuple[FutuClient, MagicMock]:
    c = FutuClient()
    c._connected = True
    c._acc_id = 12345
    c._matched_trd_env = "REAL"
    ctx = MagicMock()
    if deal_rows is not None:
        ctx.history_deal_list_query = MagicMock(return_value=(RET_OK, pd.DataFrame(deal_rows)))
    if flow_rows is not None:
        # get_acc_cash_flow is one day at a time. Yield the rows on the first
        # call (mapped to start day) and empty on every subsequent day.
        called = {"n": 0}

        def _side_effect(*args, **kwargs):
            called["n"] += 1
            if called["n"] == 1:
                return (RET_OK, pd.DataFrame(flow_rows))
            return (RET_OK, pd.DataFrame())

        ctx.get_acc_cash_flow = MagicMock(side_effect=_side_effect)
    c._trd_ctx = ctx
    return c, ctx


def _deal_row(
    deal_id: str = "d1",
    code: str = "US.AAPL",
    action: str = "BUY",
    qty: int = 10,
    price: str = "150",
    deal_time: str = "2024-05-01 10:00:00",
) -> dict:
    return {
        "deal_id": deal_id,
        "order_id": f"o-{deal_id}",
        "code": code,
        "stock_name": "Apple Inc.",
        "trd_side": action,
        "qty": float(qty),
        "price": float(price),
        "create_time": deal_time,
        "counter_broker_id": 0,
        "counter_broker_name": "",
    }


def _flow_row(
    flow_id: str = "f1",
    cashflow_type: str = "MoneyIn",
    amount: str = "1000",
    currency: str = "USD",
    flow_time: str = "2024-05-01 09:00:00",
) -> dict:
    return {
        "cashflow_id": flow_id,
        "clearing_date": flow_time,
        "cashflow_type": cashflow_type,
        "cashflow_remark": "",
        "currency": currency,
        "cashflow_amount": float(amount),
    }


def test_fetch_history_deals_returns_normalized_rows():
    c, ctx = _client_with_mocked_ctx(deal_rows=[_deal_row("d1"), _deal_row("d2", action="SELL")])
    deals = c.fetch_history_deals(
        start=datetime(2024, 5, 1, tzinfo=timezone.utc),
        end=datetime(2024, 5, 31, tzinfo=timezone.utc),
    )
    assert len(deals) == 2
    d = deals[0]
    assert d["futu_deal_id"] == "d1"
    assert d["ticker"] == "AAPL"
    assert d["futu_code"] == "US.AAPL"
    assert d["market"] == "US"
    assert d["action"] == "BUY"
    assert d["raw"]["deal_id"] == "d1"


def test_fetch_history_deals_maps_short_sides_to_buy_sell():
    """SELL_SHORT and BUY_BACK collapse to SELL/BUY for NAV-cashflow purposes."""
    c, ctx = _client_with_mocked_ctx(
        deal_rows=[
            _deal_row("d_short", action="SELL_SHORT"),
            _deal_row("d_cover", action="BUY_BACK"),
            _deal_row("d_long", action="BUY"),
        ]
    )
    deals = c.fetch_history_deals(
        start=datetime(2024, 5, 1, tzinfo=timezone.utc),
        end=datetime(2024, 5, 2, tzinfo=timezone.utc),
    )
    by_id = {d["futu_deal_id"]: d for d in deals}
    assert by_id["d_short"]["action"] == "SELL"
    assert by_id["d_cover"]["action"] == "BUY"
    assert by_id["d_long"]["action"] == "BUY"
    # Original side preserved in raw for audit
    assert by_id["d_short"]["raw"]["trd_side"] == "SELL_SHORT"


def test_fetch_history_deals_skips_unknown_trd_side():
    c, ctx = _client_with_mocked_ctx(deal_rows=[_deal_row("d_ok"), _deal_row("d_weird", action="STRADDLE")])
    deals = c.fetch_history_deals(
        start=datetime(2024, 5, 1, tzinfo=timezone.utc),
        end=datetime(2024, 5, 2, tzinfo=timezone.utc),
    )
    assert {d["futu_deal_id"] for d in deals} == {"d_ok"}


def test_fetch_history_deals_handles_fractional_seconds_in_timestamp():
    """Futu's create_time is 'YYYY-MM-DD HH:MM:SS.fff' — parser must accept."""
    c, ctx = _client_with_mocked_ctx(deal_rows=[_deal_row("d1", deal_time="2024-05-01 10:00:00.582")])
    deals = c.fetch_history_deals(
        start=datetime(2024, 5, 1, tzinfo=timezone.utc),
        end=datetime(2024, 5, 2, tzinfo=timezone.utc),
    )
    assert deals[0]["filled_at"].microsecond == 582_000


def test_fetch_history_deals_paginates_over_90_day_windows():
    c, ctx = _client_with_mocked_ctx(deal_rows=[_deal_row("d1")])
    c.fetch_history_deals(
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 12, 31, tzinfo=timezone.utc),
    )
    # 365 days / 90 = at least 5 windows (4 full + 1 partial)
    assert ctx.history_deal_list_query.call_count >= 5


def test_fetch_history_deals_raises_on_sdk_error():
    c = FutuClient()
    c._connected = True
    c._acc_id = 1
    c._matched_trd_env = "REAL"
    ctx = MagicMock()
    ctx.history_deal_list_query = MagicMock(return_value=(RET_ERROR, "rate limit exceeded"))
    c._trd_ctx = ctx
    with pytest.raises(FutuError):
        c.fetch_history_deals(
            start=datetime(2024, 5, 1, tzinfo=timezone.utc),
            end=datetime(2024, 5, 31, tzinfo=timezone.utc),
        )


def test_fetch_capital_flow_returns_normalized_rows():
    c, ctx = _client_with_mocked_ctx(
        flow_rows=[
            _flow_row("f1", cashflow_type="MoneyIn", amount="1000"),
            _flow_row("f2", cashflow_type="MoneyOut", amount="500"),
        ]
    )
    flows = c.fetch_capital_flow(
        start=datetime(2024, 5, 1, tzinfo=timezone.utc),
        end=datetime(2024, 5, 31, tzinfo=timezone.utc),
    )
    assert len(flows) == 2
    f1 = flows[0]
    assert f1["futu_flow_id"] == "f1"
    assert f1["cashflow_type"] == "DEPOSIT"
    assert f1["amount"] == 1000.0
    assert f1["currency"] == "USD"
    # outflow gets negative sign
    f2 = flows[1]
    assert f2["cashflow_type"] == "WITHDRAW"
    assert f2["amount"] == -500.0


def test_fetch_capital_flow_loops_one_call_per_weekday():
    c, ctx = _client_with_mocked_ctx(flow_rows=[_flow_row("f1")])
    c.CASHFLOW_THROTTLE_SEC = 0  # don't sleep in tests
    # 2024-05-01 (Wed) through 2024-05-10 (Fri) = 10 days, 2 weekend days
    c.fetch_capital_flow(
        start=datetime(2024, 5, 1, tzinfo=timezone.utc),
        end=datetime(2024, 5, 10, tzinfo=timezone.utc),
    )
    assert ctx.get_acc_cash_flow.call_count == 8


def test_fetch_capital_flow_skips_weekends():
    c, ctx = _client_with_mocked_ctx(flow_rows=[_flow_row("f1")])
    c.CASHFLOW_THROTTLE_SEC = 0
    # Saturday 2024-05-04 through Sunday 2024-05-05 — no weekdays, no calls
    c.fetch_capital_flow(
        start=datetime(2024, 5, 4, tzinfo=timezone.utc),
        end=datetime(2024, 5, 5, tzinfo=timezone.utc),
    )
    assert ctx.get_acc_cash_flow.call_count == 0


def test_fetch_capital_flow_raises_on_sdk_error():
    c = FutuClient()
    c._connected = True
    c._acc_id = 1
    c._matched_trd_env = "REAL"
    ctx = MagicMock()
    ctx.get_acc_cash_flow = MagicMock(return_value=(RET_ERROR, "rate limit"))
    c._trd_ctx = ctx
    with pytest.raises(FutuError):
        c.fetch_capital_flow(
            start=datetime(2024, 5, 1, tzinfo=timezone.utc),
            end=datetime(2024, 5, 2, tzinfo=timezone.utc),
        )
