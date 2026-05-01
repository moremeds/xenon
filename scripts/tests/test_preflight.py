"""Unit tests for src/xenon/execution/preflight.py (F2 server-side Gate 4)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from xenon.execution import preflight
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


def _combo_request(**overrides):
    base = dict(
        ticker="SPY",
        action="BUY",
        quantity=1,
        multiplier=100,
        legs=[
            preflight.ComboPreflightLeg(expiry="20260620", strike=500.0, right="C", action="BUY", ratio=1),
            preflight.ComboPreflightLeg(expiry="20260620", strike=510.0, right="C", action="SELL", ratio=1),
        ],
    )
    base.update(overrides)
    return preflight.ComboPreflightRequest(**base)


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


def test_stock_buy_always_ok():
    v = evaluate(
        _make_request(ticker="SPY", security_type="STK", action="BUY", quantity=100),
        PortfolioView(positions=[]),
    )
    assert v.accept is True


def test_stock_sell_no_shares_blocks():
    v = evaluate(
        _make_request(ticker="SPY", security_type="STK", action="SELL", quantity=100),
        PortfolioView(positions=[]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.INSUFFICIENT_SHARES


def test_stock_sell_within_held_ok():
    v = evaluate(
        _make_request(ticker="SPY", security_type="STK", action="SELL", quantity=100),
        PortfolioView(positions=[_stock_position("SPY", 100)]),
    )
    assert v.accept is True


def test_stock_sell_exceeds_held_blocks():
    v = evaluate(
        _make_request(ticker="SPY", security_type="STK", action="SELL", quantity=200),
        PortfolioView(positions=[_stock_position("SPY", 100)]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.INSUFFICIENT_SHARES


def test_sell_put_cash_secured_ok():
    v = evaluate(
        _make_request(
            ticker="SPY",
            security_type="OPT",
            action="SELL",
            quantity=1,
            right="P",
            expiry="20260620",
            strike=480.0,
            limit_price=5.0,
        ),
        PortfolioView(positions=[]),
    )
    assert v.accept is True


def test_index_short_call_no_cover_blocks():
    v = evaluate(
        _make_request(
            ticker="SPX",
            security_type="OPT",
            action="SELL",
            quantity=1,
            right="C",
            expiry="20260620",
            strike=5100.0,
            limit_price=10.0,
        ),
        PortfolioView(positions=[]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.INDEX_CALL_UNCOVERED


def test_index_short_call_with_same_expiry_long_call_ok():
    v = evaluate(
        _make_request(
            ticker="SPX",
            security_type="OPT",
            action="SELL",
            quantity=1,
            right="C",
            expiry="20260620",
            strike=5100.0,
            limit_price=10.0,
        ),
        PortfolioView(positions=[_long_call_position("SPX", 5000.0, "20260620")]),
    )
    assert v.accept is True


def test_index_short_call_different_expiry_long_call_blocks():
    v = evaluate(
        _make_request(
            ticker="SPX",
            security_type="OPT",
            action="SELL",
            quantity=1,
            right="C",
            expiry="20260620",
            strike=5100.0,
            limit_price=10.0,
        ),
        PortfolioView(positions=[_long_call_position("SPX", 5000.0, "20260718")]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.INDEX_CALL_UNCOVERED


def test_etf_short_call_no_cover_blocks():
    v = evaluate(
        _make_request(
            ticker="SPY",
            security_type="OPT",
            action="SELL",
            quantity=1,
            right="C",
            expiry="20260620",
            strike=500.0,
            limit_price=5.0,
        ),
        PortfolioView(positions=[]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.ETF_CALL_UNCOVERED


def test_etf_short_call_100_shares_ok():
    v = evaluate(
        _make_request(
            ticker="SPY",
            security_type="OPT",
            action="SELL",
            quantity=1,
            right="C",
            expiry="20260620",
            strike=500.0,
            limit_price=5.0,
        ),
        PortfolioView(positions=[_stock_position("SPY", 100)]),
    )
    assert v.accept is True


def test_etf_short_call_existing_short_exhausts_cover_blocks():
    v = evaluate(
        _make_request(
            ticker="SPY",
            security_type="OPT",
            action="SELL",
            quantity=1,
            right="C",
            expiry="20260620",
            strike=500.0,
            limit_price=5.0,
        ),
        PortfolioView(
            positions=[
                _stock_position("SPY", 100),
                _short_call_position("SPY", 510.0, "20260515"),
            ]
        ),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.ETF_CALL_UNCOVERED


def test_etf_short_call_vertical_spread_ok():
    v = evaluate(
        _make_request(
            ticker="SPY",
            security_type="OPT",
            action="SELL",
            quantity=1,
            right="C",
            expiry="20260620",
            strike=510.0,
            limit_price=2.0,
        ),
        PortfolioView(positions=[_long_call_position("SPY", 500.0, "20260620")]),
    )
    assert v.accept is True


def test_sell_to_close_exact_match_ok():
    v = evaluate(
        _make_request(
            ticker="SPY",
            security_type="OPT",
            action="SELL",
            quantity=1,
            right="C",
            expiry="20260620",
            strike=500.0,
            limit_price=5.0,
        ),
        PortfolioView(positions=[_long_call_position("SPY", 500.0, "20260620")]),
    )
    assert v.accept is True


def test_combo_vertical_open_allows_without_stock():
    v = preflight.evaluate_combo(_combo_request(), PortfolioView(positions=[]))
    assert v.accept is True


def test_combo_short_put_open_allows_without_stock():
    v = preflight.evaluate_combo(
        _combo_request(
            legs=[
                preflight.ComboPreflightLeg(expiry="20260620", strike=480.0, right="P", action="SELL", ratio=1),
            ]
        ),
        PortfolioView(positions=[]),
    )
    assert v.accept is True


def test_combo_short_risk_reversal_blocks_without_stock_cover():
    v = preflight.evaluate_combo(
        _combo_request(
            legs=[
                preflight.ComboPreflightLeg(expiry="20260620", strike=500.0, right="P", action="BUY", ratio=1),
                preflight.ComboPreflightLeg(expiry="20260620", strike=510.0, right="C", action="SELL", ratio=1),
            ]
        ),
        PortfolioView(positions=[]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.ETF_CALL_UNCOVERED


def test_combo_call_ratio_spread_blocks_uncovered_tail():
    v = preflight.evaluate_combo(
        _combo_request(
            legs=[
                preflight.ComboPreflightLeg(expiry="20260620", strike=500.0, right="C", action="BUY", ratio=1),
                preflight.ComboPreflightLeg(expiry="20260620", strike=510.0, right="C", action="SELL", ratio=2),
            ]
        ),
        PortfolioView(positions=[]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.ETF_CALL_UNCOVERED


def test_combo_call_ratio_spread_allows_with_stock_cover():
    v = preflight.evaluate_combo(
        _combo_request(
            legs=[
                preflight.ComboPreflightLeg(expiry="20260620", strike=500.0, right="C", action="BUY", ratio=1),
                preflight.ComboPreflightLeg(expiry="20260620", strike=510.0, right="C", action="SELL", ratio=2),
            ]
        ),
        PortfolioView(positions=[_stock_position("SPY", 100)]),
    )
    assert v.accept is True


def test_combo_closing_sell_balanced_vertical_allows_without_portfolio_cover():
    """A SELL envelope on a balanced (1 long + 1 short call) combo is safe regardless
    of portfolio because per-leg ratio analysis nets to zero uncovered shorts."""
    v = preflight.evaluate_combo(
        _combo_request(action="SELL"),
        PortfolioView(positions=[]),
    )
    assert v.accept is True


def test_combo_close_covered_by_portfolio_matches_inverse_legs():
    portfolio = PortfolioView(
        positions=[
            {
                "ticker": "QQQ",
                "structure_type": "Bull Call Spread",
                "direction": "COMBO",
                "contracts": 1,
                "expiry": "20260619",
                "legs": [
                    {"direction": "SHORT", "type": "Call", "contracts": 1, "strike": 200.0},
                    {"direction": "LONG", "type": "Call", "contracts": 1, "strike": 210.0},
                ],
            }
        ]
    )
    closing = preflight.ComboPreflightRequest(
        ticker="QQQ",
        action="SELL",
        quantity=1,
        multiplier=100,
        legs=[
            preflight.ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("200"), right="C", action="BUY", ratio=1),
            preflight.ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("210"), right="C", action="SELL", ratio=1),
        ],
    )
    assert preflight.combo_close_covered_by_portfolio(closing, portfolio) is True


def test_combo_close_covered_by_portfolio_rejects_when_no_inverse():
    closing = preflight.ComboPreflightRequest(
        ticker="QQQ",
        action="SELL",
        quantity=1,
        multiplier=100,
        legs=[
            preflight.ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("200"), right="C", action="BUY", ratio=1),
            preflight.ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("210"), right="C", action="SELL", ratio=1),
        ],
    )
    assert preflight.combo_close_covered_by_portfolio(closing, PortfolioView(positions=[])) is False


def test_combo_close_covered_by_portfolio_rejects_partial_cover():
    """Hotfix C-1: matching only one of two legs is not enough — bypass requires every leg covered."""
    portfolio = PortfolioView(
        positions=[
            {
                "ticker": "QQQ",
                "structure_type": "Short Call",
                "direction": "SHORT",
                "contracts": 1,
                "expiry": "20260619",
                "legs": [{"direction": "SHORT", "type": "Call", "contracts": 1, "strike": 200.0}],
            }
        ]
    )
    closing = preflight.ComboPreflightRequest(
        ticker="QQQ",
        action="SELL",
        quantity=1,
        multiplier=100,
        legs=[
            preflight.ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("200"), right="C", action="BUY", ratio=1),
            preflight.ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("210"), right="C", action="SELL", ratio=1),
        ],
    )
    assert preflight.combo_close_covered_by_portfolio(closing, portfolio) is False


def test_combo_close_covered_by_portfolio_aggregates_supply_across_positions():
    """Two separate positions covering one leg each combine to satisfy a 2-leg close."""
    portfolio = PortfolioView(
        positions=[
            {
                "ticker": "QQQ",
                "structure_type": "Short Call",
                "direction": "SHORT",
                "contracts": 1,
                "expiry": "20260619",
                "legs": [{"direction": "SHORT", "type": "Call", "contracts": 1, "strike": 200.0}],
            },
            {
                "ticker": "QQQ",
                "structure_type": "Long Call",
                "direction": "LONG",
                "contracts": 1,
                "expiry": "20260619",
                "legs": [{"direction": "LONG", "type": "Call", "contracts": 1, "strike": 210.0}],
            },
        ]
    )
    closing = preflight.ComboPreflightRequest(
        ticker="QQQ",
        action="SELL",
        quantity=1,
        multiplier=100,
        legs=[
            preflight.ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("200"), right="C", action="BUY", ratio=1),
            preflight.ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("210"), right="C", action="SELL", ratio=1),
        ],
    )
    assert preflight.combo_close_covered_by_portfolio(closing, portfolio) is True


def test_combo_close_covered_by_portfolio_rejects_calendar_combo():
    """Hotfix scope: calendar spreads (legs at different expiries) fall through to the gate."""
    portfolio = PortfolioView(positions=[])
    closing = preflight.ComboPreflightRequest(
        ticker="QQQ",
        action="SELL",
        quantity=1,
        multiplier=100,
        legs=[
            preflight.ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("200"), right="C", action="BUY", ratio=1),
            preflight.ComboPreflightLeg(expiry="2026-09-18", strike=Decimal("200"), right="C", action="SELL", ratio=1),
        ],
    )
    assert preflight.combo_close_covered_by_portfolio(closing, portfolio) is False


def test_etf_sell_two_calls_one_long_cover_no_shares_blocks_tail():
    """Regression for Codex pass-2 P1 #1: selling 2 calls with only 1 long-call
    cover and 0 shares must BLOCK the uncovered tail. Prior logic added
    long_cover_available back into total_cover after already consuming it in
    remaining_after_spread, so the 2nd short call incorrectly passed.
    """
    v = evaluate(
        _make_request(
            ticker="SPY",
            security_type="OPT",
            action="SELL",
            quantity=2,
            right="C",
            expiry="20260620",
            strike=510.0,
            limit_price=2.0,
        ),
        PortfolioView(positions=[_long_call_position("SPY", 500.0, "20260620")]),
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.ETF_CALL_UNCOVERED


def test_portfolio_accepts_debit_direction():
    """Regression for Codex pass-2 P1 #2: real portfolio.json positions carry
    direction=DEBIT/CREDIT/COMBO on spread structures. PortfolioPosition.direction
    must accept those strings, otherwise pydantic ValidationError fails the
    snapshot load and the gate silently goes open.
    """
    portfolio = PortfolioView(
        positions=[
            {
                "ticker": "SPX",
                "structure_type": "Bear Put Spread",
                "direction": "DEBIT",
                "contracts": 1,
                "expiry": "20260501",
                "legs": [
                    {"direction": "SHORT", "type": "Put", "contracts": 1, "strike": 7065.0},
                    {"direction": "LONG", "type": "Put", "contracts": 1, "strike": 7070.0},
                ],
            },
        ]
    )
    # Validation succeeded; verify evaluate() still works
    v = evaluate(_make_request(ticker="SPY", security_type="STK", action="BUY", quantity=1), portfolio)
    assert v.accept is True


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gate4_parity.json"


def _request_from_fixture(case: dict) -> PreflightRequest:
    r = case["request"]
    return PreflightRequest(
        ticker=r["symbol"],
        security_type="STK" if r["type"] == "stock" else "OPT",
        action=r["action"],
        quantity=r["quantity"],
        right=r.get("right"),
        expiry=r.get("expiry"),
        strike=Decimal(str(r["strike"])) if r.get("strike") is not None else None,
        multiplier=r.get("multiplier", 100),
        limit_price=Decimal(str(r["limitPrice"])),
    )


def _portfolio_from_fixture(case: dict) -> PortfolioView:
    return PortfolioView(**case["portfolio"])


@pytest.mark.parametrize("case", json.loads(FIXTURE_PATH.read_text())["cases"], ids=lambda c: c["name"])
def test_parity_fixture(case):
    req = _request_from_fixture(case)
    portfolio = _portfolio_from_fixture(case)
    verdict = evaluate(req, portfolio)

    expected = case["expected"]
    assert verdict.accept == expected["accept"], (
        f"{case['name']}: expected accept={expected['accept']}, got {verdict.accept} (reason={verdict.reason_code})"
    )
    if expected["reason_code"] is None:
        assert verdict.reason_code is None
    else:
        assert verdict.reason_code == ReasonCode(expected["reason_code"])
