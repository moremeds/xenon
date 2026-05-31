"""position_key encoding tests. Spec §5.3."""
from __future__ import annotations

from xenon.execution.brackets.position_key import compute_position_key


def _leg(sec_type, symbol, expiry=None, strike=None, right=None, action="BUY", ratio=1):
    return {
        "sec_type": sec_type,
        "symbol": symbol,
        "expiry": expiry,
        "strike": strike,
        "right": right,
        "action": action,
        "ratio": ratio,
        "fill_price": 1.0,
        "con_id": 0,
    }


def test_stock_key():
    key = compute_position_key("stock", {"legs": [_leg("STK", "AAPL")]})
    assert key == "STK::AAPL"


def test_long_option_key():
    key = compute_position_key(
        "long_option",
        {"legs": [_leg("OPT", "GOOG", "20260417", 315.0, "C")]},
    )
    assert key == "OPT::GOOG::20260417::315::C"


def test_credit_spread_key():
    legs = [
        _leg("OPT", "SPY", "20260516", 580.0, "P", action="SELL"),
        _leg("OPT", "SPY", "20260516", 575.0, "P", action="BUY"),
    ]
    key = compute_position_key("credit_spread", {"legs": legs})
    assert key == "CS::SPY::20260516::580::575::P"


def test_credit_spread_key_leg_order_invariant():
    legs_a = [
        _leg("OPT", "SPY", "20260516", 575.0, "P", action="BUY"),
        _leg("OPT", "SPY", "20260516", 580.0, "P", action="SELL"),
    ]
    legs_b = [
        _leg("OPT", "SPY", "20260516", 580.0, "P", action="SELL"),
        _leg("OPT", "SPY", "20260516", 575.0, "P", action="BUY"),
    ]
    assert compute_position_key("credit_spread", {"legs": legs_a}) == compute_position_key(
        "credit_spread", {"legs": legs_b}
    )


def test_debit_combo_key_hashed_and_leg_order_invariant():
    legs_a = [
        _leg("OPT", "TSLA", "20260516", 200.0, "C", action="BUY"),
        _leg("OPT", "TSLA", "20260516", 210.0, "C", action="SELL"),
    ]
    legs_b = list(reversed(legs_a))
    key_a = compute_position_key("debit_combo", {"legs": legs_a})
    key_b = compute_position_key("debit_combo", {"legs": legs_b})
    assert key_a == key_b
    assert key_a.startswith("COMBO::")
    assert key_a.endswith("::TSLA")


def test_covered_call_key():
    legs = [
        _leg("STK", "AAPL", action="BUY"),
        _leg("OPT", "AAPL", "20260620", 200.0, "C", action="SELL"),
    ]
    key = compute_position_key("covered_call", {"legs": legs})
    assert key == "CC::AAPL::20260620::200"
