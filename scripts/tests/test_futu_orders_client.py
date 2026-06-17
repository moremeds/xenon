"""FutuClient order fetchers: frame normalization, side mapping, fee batching."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from xenon.clients.futu_client import FutuClient


def _client(ctx):
    c = FutuClient()
    c._connected = True
    c._acc_id = 1
    c._matched_trd_env = "REAL"
    c._trd_ctx = ctx
    return c


@pytest.fixture(autouse=True)
def _ret_ok(monkeypatch):
    # RET_OK is 0 in the futu SDK; ensure the module sentinel matches for these unit tests.
    monkeypatch.setattr("xenon.clients.futu_client.RET_OK", 0, raising=False)


def test_fetch_open_orders_normalizes_row():
    frame = pd.DataFrame(
        [
            {
                "order_id": "O1",
                "code": "US.QQQ",
                "trd_side": "BUY",
                "order_type": "NORMAL",
                "qty": 1,
                "price": 630.96,
                "aux_price": 0.0,
                "order_status": "SUBMITTED",
                "time_in_force": "GTC",
                "dealt_qty": 0,
                "dealt_avg_price": 0.0,
                "create_time": "2026-06-17 09:30:00",
                "updated_time": "2026-06-17 09:31:00",
            }
        ]
    )
    ctx = MagicMock()
    ctx.order_list_query.return_value = (0, frame)
    rows = _client(ctx).fetch_open_orders()
    assert len(rows) == 1
    r = rows[0]
    assert r["futu_order_id"] == "O1"
    assert r["action"] == "BUY"
    assert r["order_type"] == "NORMAL"
    assert r["status"] == "SUBMITTED"
    assert r["tif"] == "GTC"
    assert r["limit_price"] == 630.96
    assert r["aux_price"] is None  # 0.0 → None
    assert r["ticker"] == "QQQ"
    assert r["market"] == "US"
    assert r["created_at"].year == 2026


def test_sell_short_normalizes_to_sell_and_market_order_has_no_limit():
    frame = pd.DataFrame(
        [
            {
                "order_id": "O2",
                "code": "US.AAPL",
                "trd_side": "SELL_SHORT",
                "order_type": "MARKET",
                "qty": 3,
                "price": 0.0,
                "aux_price": 0.0,
                "order_status": "FILLED_ALL",
                "time_in_force": "DAY",
                "dealt_qty": 3,
                "dealt_avg_price": 200.5,
                "create_time": "2026-06-17 10:00:00",
                "updated_time": "2026-06-17 10:00:05",
            }
        ]
    )
    ctx = MagicMock()
    ctx.order_list_query.return_value = (0, frame)
    r = _client(ctx).fetch_open_orders()[0]
    assert r["action"] == "SELL"  # SELL_SHORT → SELL
    assert r["limit_price"] is None  # price 0 → market
    assert r["filled_qty"] == 3
    assert r["avg_fill_price"] == 200.5


def test_na_sentinel_numeric_fields_coerce_to_none():
    """Futu returns the string 'N/A' for unset numeric fields (no aux/trail on a
    plain limit, no fill price before execution). float('N/A') used to crash the
    whole fetch_open_orders → the sync aborted and nothing synced. All numeric
    fields must tolerate 'N/A' and any non-numeric junk → None / 0."""
    frame = pd.DataFrame(
        [
            {
                "order_id": "O9",
                "code": "US.QQQ",
                "trd_side": "BUY",
                "order_type": "NORMAL",
                "qty": 2,
                "price": 631.5,
                "aux_price": "N/A",  # plain limit → no trail/stop price
                "order_status": "SUBMITTED",
                "time_in_force": "GTC",
                "dealt_qty": "N/A",
                "dealt_avg_price": "N/A",  # not filled yet
                "create_time": "2026-06-17 09:30:00",
                "updated_time": "2026-06-17 09:31:00",
            }
        ]
    )
    ctx = MagicMock()
    ctx.order_list_query.return_value = (0, frame)
    r = _client(ctx).fetch_open_orders()[0]  # must not raise
    assert r["limit_price"] == 631.5
    assert r["aux_price"] is None
    assert r["filled_qty"] == 0
    assert r["avg_fill_price"] is None
    assert r["quantity"] == 2


def test_na_sentinel_price_field_coerces_without_crash():
    """`price='N/A'` (e.g. a market order with no limit) must not crash; → no limit."""
    frame = pd.DataFrame(
        [
            {
                "order_id": "O10",
                "code": "US.AAPL",
                "trd_side": "SELL",
                "order_type": "MARKET",
                "qty": 1,
                "price": "N/A",
                "aux_price": "N/A",
                "order_status": "FILLED_ALL",
                "time_in_force": "DAY",
                "dealt_qty": 1,
                "dealt_avg_price": 201.0,
                "create_time": "2026-06-17 10:00:00",
                "updated_time": "2026-06-17 10:00:05",
            }
        ]
    )
    ctx = MagicMock()
    ctx.order_list_query.return_value = (0, frame)
    r = _client(ctx).fetch_open_orders()[0]  # must not raise
    assert r["limit_price"] is None
    assert r["filled_qty"] == 1
    assert r["avg_fill_price"] == 201.0


def test_fetch_open_orders_empty_frame_returns_empty():
    ctx = MagicMock()
    ctx.order_list_query.return_value = (0, pd.DataFrame())
    assert _client(ctx).fetch_open_orders() == []


def test_fetch_order_fees_batches_and_maps_fee_amount(monkeypatch):
    frame = pd.DataFrame([{"order_id": "O1", "fee_amount": 0.75, "fee_details": "[]"}])
    ctx = MagicMock()
    ctx.order_fee_query.return_value = (0, frame)
    c = _client(ctx)
    c.FEE_THROTTLE_SEC = 0  # no sleep in tests
    rows = c.fetch_order_fees(["O1"])
    assert rows == [
        {
            "futu_order_id": "O1",
            "total_fee": 0.75,
            "currency": "USD",
            "raw": {"order_id": "O1", "fee_amount": 0.75, "fee_details": "[]"},
        }
    ]
    ctx.order_fee_query.assert_called_once()


def test_fetch_order_fees_tolerates_list_valued_fee_details(monkeypatch):
    """order_fee_query returns `fee_details` as a LIST per row. The raw-cell NaN
    scrub used `pd.isna(v)` which returns an ARRAY for list cells → `if array`
    raised ValueError, aborting fetch_order_fees and the whole sync before the
    closed-trade rebuild ever ran. List cells must pass through untouched."""
    details = [{"title": "commission", "value": 0.5}, {"title": "platform", "value": 0.25}]
    frame = pd.DataFrame([{"order_id": "O1", "fee_amount": 0.75, "fee_details": details}])
    ctx = MagicMock()
    ctx.order_fee_query.return_value = (0, frame)
    c = _client(ctx)
    c.FEE_THROTTLE_SEC = 0
    rows = c.fetch_order_fees(["O1"])  # must not raise on the multi-element list cell
    assert rows[0]["futu_order_id"] == "O1"
    assert rows[0]["total_fee"] == 0.75
    assert rows[0]["raw"]["fee_details"] == details


def test_fetch_order_fees_na_sentinel_fee_amount_coerces_to_zero(monkeypatch):
    """A non-numeric `fee_amount` sentinel ('N/A') must not crash the fee fetch.

    `float(row.get("fee_amount") or 0)` raised ValueError on 'N/A', aborting
    fetch_order_fees and the whole orders sync before the closed-trade rebuild.
    _coerce_num maps the sentinel to None → total_fee falls back to 0.0."""
    frame = pd.DataFrame([{"order_id": "O1", "fee_amount": "N/A", "fee_details": "[]"}])
    ctx = MagicMock()
    ctx.order_fee_query.return_value = (0, frame)
    c = _client(ctx)
    c.FEE_THROTTLE_SEC = 0
    rows = c.fetch_order_fees(["O1"])  # must not raise on the 'N/A' sentinel
    assert rows[0]["futu_order_id"] == "O1"
    assert rows[0]["total_fee"] == 0.0


def test_fetch_order_fees_empty_input_no_call():
    ctx = MagicMock()
    assert _client(ctx).fetch_order_fees([]) == []
    ctx.order_fee_query.assert_not_called()
