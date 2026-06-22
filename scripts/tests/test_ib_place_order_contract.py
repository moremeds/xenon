"""ib_place_order builds the right Stock contract from the order body.

Real IB contracts (2026-06-22): AAPL (NASDAQ/SMART, USD), 5016 (TSEJ, JPY).
"""

from xenon.execution.ib_place_order import _build_stock_contract


def test_build_stock_contract_defaults_to_smart_usd():
    c = _build_stock_contract({"symbol": "AAPL"})
    assert c.symbol == "AAPL"
    assert c.exchange == "SMART"
    assert c.currency == "USD"


def test_build_stock_contract_uses_body_exchange_currency():
    c = _build_stock_contract({"symbol": "5016", "exchange": "TSEJ", "currency": "JPY"})
    assert c.symbol == "5016"
    assert c.exchange == "TSEJ"
    assert c.currency == "JPY"


def test_build_stock_contract_uppercases():
    c = _build_stock_contract({"symbol": "5016", "exchange": "tsej", "currency": "jpy"})
    assert c.exchange == "TSEJ"
    assert c.currency == "JPY"
