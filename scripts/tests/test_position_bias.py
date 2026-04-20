"""Tests for per-position directional bias inference.

Covers the NVDA-class bug (Long Put misclassified as bullish), broker-shape
canonicalization (IB structure_type vs Futu normalized dict), and non-
directional structures (iron condor, collar, straddle).
"""

from xenon.utils.position_bias import position_bias


def _ib(structure, structure_type, direction="LONG", legs=None):
    return {
        "ticker": "NVDA",
        "direction": direction,
        "structure": structure,
        "qty": 1,
        "raw": {
            "structure_type": structure_type,
            "direction": direction,
            "legs": legs or [],
        },
    }


def _futu(kind, right=None, position_side="LONG", strike=100.0):
    return {
        "ticker": "TSLA",
        "direction": position_side,
        "structure": "Unknown",
        "qty": 1,
        "raw": {
            "normalized": {"kind": kind, "right": right, "strike": strike},
            "position_side": position_side,
        },
    }


# ── IB canonicalization via raw.structure_type ────────────────────────────
class TestIBCanonicalization:
    def test_long_call_is_bullish(self):
        assert position_bias(_ib("Long Call $185.0", "Long Call")) == "bullish"

    def test_long_put_is_bearish(self):
        """The NVDA bug: long put previously misclassified as bullish."""
        assert position_bias(_ib("Long Put $160.0", "Long Put", direction="LONG")) == "bearish"

    def test_short_put_is_bullish(self):
        """Short put (cash-secured) is income but directionally bullish."""
        bias = position_bias(_ib("Short Put $170.0", "Short Put", direction="SHORT"))
        # May be classified as 'income' or 'bullish'; for the NVDA bug the key
        # assertion is that it is NOT 'bearish'. We tighten to 'bullish' since
        # options-structures.json categorises Short Put (Cash-Secured) as bullish.
        assert bias == "bullish"

    def test_bear_put_spread_is_bearish(self):
        assert position_bias(_ib("Bear Put Spread", "Bear Put Spread")) == "bearish"

    def test_iron_condor_is_income(self):
        assert position_bias(_ib("Iron Condor", "Iron Condor")) == "income"

    def test_long_stock_is_bullish(self):
        assert position_bias(_ib("Long Stock", "Long Stock")) == "bullish"

    def test_collar_is_hedge(self):
        assert position_bias(_ib("Collar", "Collar")) == "hedge"

    def test_long_straddle_is_neutral_vol(self):
        assert position_bias(_ib("Long Straddle", "Long Straddle")) == "neutral_vol"


# ── Futu canonicalization via raw.normalized ──────────────────────────────
class TestFutuCanonicalization:
    def test_futu_long_call(self):
        assert position_bias(_futu("OPT", right="C", position_side="LONG")) == "bullish"

    def test_futu_long_put(self):
        assert position_bias(_futu("OPT", right="P", position_side="LONG")) == "bearish"

    def test_futu_short_put(self):
        assert position_bias(_futu("OPT", right="P", position_side="SHORT")) == "bullish"

    def test_futu_short_call(self):
        """Short call alone is bearish (not covered by long stock here)."""
        assert position_bias(_futu("OPT", right="C", position_side="SHORT")) == "bearish"

    def test_futu_long_stock(self):
        assert position_bias(_futu("STK", position_side="LONG")) == "bullish"

    def test_futu_short_stock(self):
        assert position_bias(_futu("STK", position_side="SHORT")) == "bearish"


# ── Fallback + unknown ────────────────────────────────────────────────────
class TestFallback:
    def test_unknown_structure_returns_unknown_not_bullish(self):
        """The critical rule: never default unknown to bullish."""
        pos = {
            "ticker": "XXX",
            "direction": "LONG",
            "structure": "Some Exotic Thing",
            "qty": 1,
            "raw": {},
        }
        assert position_bias(pos) == "unknown"

    def test_fall_back_from_structure_label_when_no_raw(self):
        pos = {
            "ticker": "X",
            "direction": "LONG",
            "structure": "Long Call",
            "qty": 1,
            "raw": {},
        }
        assert position_bias(pos) == "bullish"
