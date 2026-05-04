"""Asset-class classifier tests. Spec §3.1, §6.2."""
from __future__ import annotations

from xenon.execution.brackets.asset_class import (
    AssetClass,
    classify_position,
)


def _stk(symbol, action="BUY"):
    return {
        "sec_type": "STK",
        "symbol": symbol,
        "action": action,
        "ratio": 1,
        "fill_price": 100.0,
        "con_id": 0,
    }


def _opt(symbol, expiry, strike, right, action="BUY", ratio=1):
    return {
        "sec_type": "OPT",
        "symbol": symbol,
        "expiry": expiry,
        "strike": strike,
        "right": right,
        "action": action,
        "ratio": ratio,
        "fill_price": 5.0,
        "con_id": 0,
    }


def test_stock_long():
    result = classify_position(legs=[_stk("AAPL")], wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.STOCK


def test_long_option():
    result = classify_position(
        legs=[_opt("GOOG", "20260417", 315.0, "C")],
        wizard_session_payload=None,
        sibling_legs=None,
    )
    assert result.asset_class == AssetClass.LONG_OPTION


def test_credit_spread_short_put():
    legs = [
        _opt("SPY", "20260516", 580.0, "P", action="SELL"),
        _opt("SPY", "20260516", 575.0, "P", action="BUY"),
    ]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.CREDIT_SPREAD


def test_credit_spread_short_call():
    legs = [
        _opt("SPY", "20260516", 590.0, "C", action="SELL"),
        _opt("SPY", "20260516", 595.0, "C", action="BUY"),
    ]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.CREDIT_SPREAD


def test_debit_combo_call_vertical():
    legs = [
        _opt("TSLA", "20260516", 200.0, "C", action="BUY"),
        _opt("TSLA", "20260516", 210.0, "C", action="SELL"),
    ]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.DEBIT_COMBO


def test_covered_call_pattern():
    legs = [
        _stk("AAPL"),
        _opt("AAPL", "20260620", 200.0, "C", action="SELL"),
    ]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.COVERED_CALL


def test_jade_lizard_unclassified():
    legs = [
        _opt("TSLA", "20260516", 210.0, "C", action="BUY"),
        _opt("TSLA", "20260516", 220.0, "C", action="SELL"),
        _opt("TSLA", "20260516", 180.0, "P", action="SELL"),
    ]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    assert result.asset_class == AssetClass.UNCLASSIFIED


def test_wizard_session_overrides_pattern_match():
    """When a combo wizard session_id is present, defer to its declared structure."""
    legs = [_opt("GOOG", "20260417", 315.0, "C")]
    payload = {"asset_class": "debit_combo"}
    result = classify_position(legs=legs, wizard_session_payload=payload, sibling_legs=None)
    assert result.asset_class == AssetClass.DEBIT_COMBO


def test_manual_leg_by_leg_detection_returns_unclassified():
    """Single-leg fill with sibling fills at same scope+symbol+expiry is unsupported."""
    legs = [_opt("SPY", "20260516", 580.0, "P", action="SELL")]
    sibling_legs = [_opt("SPY", "20260516", 575.0, "P", action="BUY")]
    result = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=sibling_legs)
    assert result.asset_class == AssetClass.UNCLASSIFIED
    assert result.reason == "manual_multi_leg_unsupported"
