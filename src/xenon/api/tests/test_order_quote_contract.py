"""The order-quote contract builder honors exchange/currency for foreign stocks.

Real IB contracts (2026-06-22): AAPL (SMART/USD), 5016 (TSEJ/JPY).
"""

from xenon.api.server import _contract_from_order_body


def test_contract_from_body_stock_defaults_smart_usd():
    c = _contract_from_order_body({"type": "stock", "symbol": "aapl"})
    assert c.symbol == "AAPL"
    assert c.exchange == "SMART"
    assert c.currency == "USD"


def test_contract_from_body_foreign_stock_uses_exchange_currency():
    c = _contract_from_order_body({"type": "stock", "symbol": "5016", "exchange": "TSEJ", "currency": "JPY"})
    assert c.symbol == "5016"
    assert c.exchange == "TSEJ"
    assert c.currency == "JPY"


def test_contract_from_body_option_stays_smart_usd():
    c = _contract_from_order_body(
        {
            "type": "option",
            "symbol": "SPY",
            "expiry": "20260619",
            "strike": 600,
            "right": "C",
        }
    )
    assert c.exchange == "SMART"
    assert c.currency == "USD"
