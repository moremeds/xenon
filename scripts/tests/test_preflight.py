"""Unit tests for src/xenon/execution/preflight.py (F2 server-side Gate 4)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from xenon.execution.preflight import (
    PortfolioView,
    PreflightRequest,
    ReasonCode,
    Verdict,
    evaluate,
)


def _stock_position(ticker: str, contracts: int) -> dict:
    return {
        "ticker": ticker,
        "structure_type": "Stock",
        "direction": "LONG",
        "contracts": contracts,
        "expiry": None,
        "legs": [{"direction": "LONG", "type": "Stock", "contracts": contracts, "strike": 0.0}],
    }


def _long_call_position(ticker: str, strike: float, expiry: str, contracts: int = 1) -> dict:
    return {
        "ticker": ticker,
        "structure_type": "Long Call",
        "direction": "LONG",
        "contracts": contracts,
        "expiry": expiry,
        "legs": [{"direction": "LONG", "type": "Call", "contracts": contracts, "strike": strike}],
    }


def _short_call_position(ticker: str, strike: float, expiry: str, contracts: int = 1) -> dict:
    return {
        "ticker": ticker,
        "structure_type": "Short Call",
        "direction": "SHORT",
        "contracts": contracts,
        "expiry": expiry,
        "legs": [{"direction": "SHORT", "type": "Call", "contracts": contracts, "strike": strike}],
    }


def _make_request(**overrides) -> PreflightRequest:
    base = dict(
        ticker="SPY",
        security_type="STK",
        action="BUY",
        quantity=1,
        right=None,
        expiry=None,
        strike=None,
        multiplier=100,
        limit_price=500.0,
    )
    base.update(overrides)
    return PreflightRequest(**base)


def test_universe_unknown_ticker_blocks():
    verdict = evaluate(_make_request(ticker="AAPL"), PortfolioView(positions=[]))
    assert verdict.accept is False
    assert verdict.reason_code == ReasonCode.UNIVERSE_UNKNOWN


def test_index_stk_buy_blocks():
    verdict = evaluate(
        _make_request(ticker="SPX", security_type="STK", action="BUY"),
        PortfolioView(positions=[]),
    )
    assert verdict.accept is False
    assert verdict.reason_code == ReasonCode.INDEX_HAS_NO_STOCK


def test_index_stk_sell_blocks():
    verdict = evaluate(
        _make_request(ticker="NDX", security_type="STK", action="SELL"),
        PortfolioView(positions=[]),
    )
    assert verdict.accept is False
    assert verdict.reason_code == ReasonCode.INDEX_HAS_NO_STOCK
