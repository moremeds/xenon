"""Tests for scripts/utils/portfolio_adapter.py — broker-agnostic position normalization."""
import json
from pathlib import Path

import pytest

from utils.portfolio_adapter import (
    NormalizedPosition,
    group_by_ticker,
    load_normalized_positions,
)


@pytest.fixture
def ib_portfolio_file(tmp_path: Path) -> Path:
    p = tmp_path / "portfolio.json"
    p.write_text(json.dumps({
        "positions": [
            {"id": 1, "ticker": "GLD", "structure": "Short Put $440.0",
             "direction": "SHORT", "contracts": 1},
            {"id": 2, "ticker": "QQQ", "structure": "Long Call $500.0",
             "direction": "LONG", "contracts": 2},
        ]
    }))
    return p


@pytest.fixture
def futu_portfolio_file(tmp_path: Path) -> Path:
    p = tmp_path / "futu_portfolio.json"
    p.write_text(json.dumps({
        "positions": [
            {
                "futu_code": "US.TSLA270115P400000",
                "normalized": {"kind": "OPT", "symbol": "TSLA", "right": "P", "strike": 400.0,
                               "expiry": "20270115", "currency": "USD"},
                "quantity": -15.0, "position_side": "SHORT",
            },
            {
                "futu_code": "US.NVDA",
                "normalized": {"kind": "STK", "symbol": "NVDA", "currency": "USD"},
                "quantity": 100.0, "position_side": "LONG",
            },
            {
                "futu_code": "HK.00700",
                "normalized": {"kind": "STK", "symbol": "0700", "currency": "HKD"},
                "quantity": 200.0, "position_side": "LONG",
            },
        ]
    }))
    return p


def test_load_ib(monkeypatch, ib_portfolio_file):
    from utils import portfolio_adapter
    monkeypatch.setattr(portfolio_adapter, "IB_PORTFOLIO", ib_portfolio_file)
    result = load_normalized_positions("ib")
    assert len(result.positions) == 2
    assert result.positions[0].ticker == "GLD"
    assert result.positions[0].direction == "SHORT"
    assert result.positions[0].structure == "Short Put $440.0"
    assert result.positions[1].ticker == "QQQ"
    assert result.positions[1].direction == "LONG"
    assert result.skipped_unsupported == 0


def test_load_futu_filters_non_us(monkeypatch, futu_portfolio_file):
    from utils import portfolio_adapter
    monkeypatch.setattr(portfolio_adapter, "FUTU_PORTFOLIO", futu_portfolio_file)
    result = load_normalized_positions("futu")
    tickers = [p.ticker for p in result.positions]
    assert "TSLA" in tickers
    assert "NVDA" in tickers
    assert "0700" not in tickers  # HK skipped
    assert result.skipped_unsupported == 1
    tsla = next(p for p in result.positions if p.ticker == "TSLA")
    assert tsla.direction == "SHORT"
    assert "Put" in tsla.structure or "P" in tsla.structure


def test_load_missing_file_returns_empty(monkeypatch, tmp_path):
    from utils import portfolio_adapter
    monkeypatch.setattr(portfolio_adapter, "FUTU_PORTFOLIO", tmp_path / "nope.json")
    result = load_normalized_positions("futu")
    assert result.positions == []
    assert result.skipped_unsupported == 0


def test_unknown_account_raises():
    with pytest.raises(ValueError):
        load_normalized_positions("etrade")  # type: ignore[arg-type]


def test_group_by_ticker_dedupes():
    positions = [
        NormalizedPosition(ticker="TSLA", direction="LONG", structure="Stock", qty=100, raw={}),
        NormalizedPosition(ticker="TSLA", direction="SHORT", structure="Short Put", qty=-1, raw={}),
        NormalizedPosition(ticker="NVDA", direction="LONG", structure="Stock", qty=50, raw={}),
    ]
    grouped = group_by_ticker(positions)
    assert set(grouped.keys()) == {"TSLA", "NVDA"}
    assert len(grouped["TSLA"]) == 2
    assert len(grouped["NVDA"]) == 1
