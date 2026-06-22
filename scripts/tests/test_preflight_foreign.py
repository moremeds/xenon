"""Preflight must let foreign cash equities past the US-only universe gate.

Real IB contracts / prices, 2026-06-22 (no synthetic data):
  5016   JX Advanced Metals — TSEJ/JPY, last ¥5,267
  000660 SK Hynix          — KRX/KRW, last ₩2,885,000
"""

from decimal import Decimal

from xenon.execution.preflight import (
    PortfolioView,
    PreflightRequest,
    ReasonCode,
    evaluate,
)


def _view(positions=None):
    return PortfolioView(positions=positions or [])


def test_foreign_stock_buy_bypasses_universe():
    req = PreflightRequest(
        ticker="5016",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("5267"),
        currency="JPY",
        exchange="TSEJ",
    )
    v = evaluate(req, _view())
    assert v.accept is True


def test_foreign_stock_sell_without_shares_still_blocked():
    req = PreflightRequest(
        ticker="000660",
        security_type="STK",
        action="SELL",
        quantity=10,
        limit_price=Decimal("2885000"),
        currency="KRW",
        exchange="KRX",
    )
    v = evaluate(req, _view())
    assert v.accept is False
    assert v.reason_code == ReasonCode.INSUFFICIENT_SHARES


def test_unknown_us_ticker_still_rejected():
    # USD tickers outside the V1 universe must STILL be rejected.
    req = PreflightRequest(
        ticker="ZZZZ",
        security_type="STK",
        action="BUY",
        quantity=1,
        limit_price=Decimal("1"),
        currency="USD",
    )
    v = evaluate(req, _view())
    assert v.accept is False
    assert v.reason_code == ReasonCode.UNIVERSE_UNKNOWN
