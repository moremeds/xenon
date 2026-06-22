"""Tests for scripts/utils/symbol_norm.py.

Per tribunal T16, the critical property is the *semantic* round-trip
on quoting fields, not a string-identity round-trip.
"""

from __future__ import annotations

import pytest

from xenon.utils.symbol_norm import futu_to_ib, ib_to_futu

# ────────────────────────────────────────────────────────────────────
# Stocks
# ────────────────────────────────────────────────────────────────────


def test_plain_us_stock():
    r = futu_to_ib("US.AAPL")
    assert r["kind"] == "STK"
    assert r["symbol"] == "AAPL"
    assert r["exchange"] == "SMART"
    assert r["currency"] == "USD"
    assert r["live_data"] is True


def test_us_dotted_ticker_brk_b():
    r = futu_to_ib("US.BRK.B")
    assert r["kind"] == "STK"
    assert r["symbol"] == "BRK B"  # IB uses space, not dot


def test_us_dotted_ticker_brk_a():
    r = futu_to_ib("US.BRK.A")
    assert r["kind"] == "STK"
    assert r["symbol"] == "BRK A"


# ────────────────────────────────────────────────────────────────────
# Options
# ────────────────────────────────────────────────────────────────────


def test_us_option_call_6_digit_strike():
    # AAPL 2024-01-19 $190 Call, Futu "short" strike form (×1000, 6 digits)
    r = futu_to_ib("US.AAPL240119C190000")
    assert r["kind"] == "OPT"
    assert r["symbol"] == "AAPL"
    assert r["expiry"] == "20240119"
    assert r["strike"] == 190.0
    assert r["right"] == "C"
    assert r["live_data"] is False  # v1: no IB qualification
    assert r["trading_class"] is None


def test_us_option_put_8_digit_strike():
    # TSLA 2026-06-19 $250.50 Put, 8-digit strike form
    r = futu_to_ib("US.TSLA260619P00250500")
    assert r["kind"] == "OPT"
    assert r["symbol"] == "TSLA"
    assert r["expiry"] == "20260619"
    assert r["strike"] == 250.5
    assert r["right"] == "P"


def test_us_option_fractional_strike():
    # SPY 2026-03-20 $567.50 Call
    r = futu_to_ib("US.SPY260320C567500")
    assert r["strike"] == 567.5


def test_option_pattern_not_confused_with_stock():
    # Must not match the option regex — no date digits
    r = futu_to_ib("US.META")
    assert r["kind"] == "STK"


# ────────────────────────────────────────────────────────────────────
# Foreign cash equities (Japan) — classified as STK, not UNKNOWN
# ────────────────────────────────────────────────────────────────────


def test_japan_stock_is_classified_as_stock():
    # JP.6981 = Murata Manufacturing (Tokyo Stock Exchange), priced in JPY.
    # Verified present in the live Futu snapshot 2026-06-22. Must NOT fall
    # through to UNKNOWN — it's an ordinary long cash equity.
    r = futu_to_ib("JP.6981")
    assert r["kind"] == "STK"
    assert r["symbol"] == "6981"  # bare ticker, market prefix stripped (US.AAPL → AAPL)
    assert r["exchange"] == "TSEJ"  # IB's Tokyo venue code
    assert r["currency"] == "JPY"
    assert r["market"] == "JP"  # preserved so ib_to_futu reconstructs JP.6981
    # No live IB quote routing is wired for foreign Futu rows (the foreign
    # quote path needs an exchange-qualified subscription, not SMART/USD).
    assert r["live_data"] is False


def test_japan_numeric_ticker_stays_string():
    # Leading-zero / digit-only tickers must never be coerced to a number.
    r = futu_to_ib("JP.7203")  # Toyota
    assert r["kind"] == "STK"
    assert r["symbol"] == "7203"
    assert isinstance(r["symbol"], str)


def test_us_stock_carries_us_market_tag():
    # The market tag is now present on every STK result for round-trip use.
    r = futu_to_ib("US.AAPL")
    assert r["market"] == "US"


# ────────────────────────────────────────────────────────────────────
# UNKNOWN cases
# ────────────────────────────────────────────────────────────────────


def test_hk_market_is_unknown_in_v1():
    r = futu_to_ib("HK.00700")
    assert r["kind"] == "UNKNOWN"
    assert "HK" in r["reason"]
    assert r["raw"] == "HK.00700"


def test_cn_sh_market_unknown():
    r = futu_to_ib("SH.600519")
    assert r["kind"] == "UNKNOWN"
    assert "SH" in r["reason"]


def test_missing_market_prefix_is_unknown():
    r = futu_to_ib("AAPL")
    assert r["kind"] == "UNKNOWN"
    assert "market" in r["reason"]


def test_empty_string_is_unknown():
    r = futu_to_ib("")
    assert r["kind"] == "UNKNOWN"


def test_none_is_unknown_not_crash():
    r = futu_to_ib(None)  # type: ignore[arg-type]
    assert r["kind"] == "UNKNOWN"


def test_garbage_ticker_returns_stock_not_crash():
    # US.FOO123 does NOT match option regex (wrong date shape),
    # falls through to stock. That's acceptable — the STK path
    # just becomes "FOO123" and will fail at quote time with no
    # crash cost.
    r = futu_to_ib("US.FOO")
    assert r["kind"] == "STK"


def test_never_returns_none_never_raises():
    # Exhaustive fuzz sample
    inputs = ["", "US.", "X", "HK.", "US..", ".", "US.AAPL.X", None, 42]
    for inp in inputs:
        result = futu_to_ib(inp)  # type: ignore[arg-type]
        assert result is not None
        assert "kind" in result


# ────────────────────────────────────────────────────────────────────
# Semantic round-trip (tribunal T16)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "futu_code",
    [
        "US.AAPL",
        "US.TSLA",
        "US.BRK.B",
        "US.AAPL240119C190000",
        "US.SPY260320P00450000",
        "US.TSLA260619P00250500",
        "US.NVDA260116C01000000",  # $1000 strike
        "JP.6981",  # foreign cash equity must round-trip back to JP.6981
    ],
)
def test_semantic_round_trip(futu_code):
    first = futu_to_ib(futu_code)
    assert first["kind"] != "UNKNOWN"
    reserialized = ib_to_futu(first)
    second = futu_to_ib(reserialized)
    # Compare on quoting fields only — string round-trip would fail on
    # 6-digit vs 8-digit strike padding, which is intentional.
    assert first["kind"] == second["kind"]
    assert first["symbol"] == second["symbol"]
    if first["kind"] == "OPT":
        assert first["expiry"] == second["expiry"]
        assert first["strike"] == second["strike"]
        assert first["right"] == second["right"]


def test_ib_to_futu_rejects_unknown():
    with pytest.raises(ValueError):
        ib_to_futu({"kind": "UNKNOWN", "raw": "?", "reason": "x", "live_data": False})
