"""Tests for shared dark-pool + options-flow summarizers.

Covers:
- summarize_dark_pool scoring and signal buckets
- options-conflict -25 penalty (bug fix: combined_bias -> bias, 5-state enum)
- edge cases (no data, short lookback, low print count)
"""

import pytest
from analysis.dark_pool_summary import summarize_dark_pool
from analysis.options_flow_summary import summarize_options_flow


def _flow(direction="ACCUMULATION", strength=60.0, buy_ratio=0.8, num_prints=200, daily=None, options_bias=None):
    """Build a minimal flow_data dict shaped like fetch_flow.fetch_flow output."""
    return {
        "dark_pool": {
            "aggregate": {
                "flow_direction": direction,
                "flow_strength": strength,
                "dp_buy_ratio": buy_ratio,
                "num_prints": num_prints,
            },
            "daily": daily or [{"flow_direction": direction, "flow_strength": strength}],
        },
        "options_flow": {"bias": options_bias} if options_bias is not None else {},
    }


class TestDarkPoolScoring:
    def test_strong_accumulation_clears_strong_threshold(self):
        out = summarize_dark_pool(_flow(strength=70, num_prints=500))
        assert out["signal"] == "STRONG"
        assert out["direction"] == "ACCUMULATION"
        assert out["score"] >= 60

    def test_moderate_band(self):
        out = summarize_dark_pool(_flow(strength=45, num_prints=200))
        assert out["signal"] == "MODERATE"

    def test_weak_when_directional_but_low_score(self):
        out = summarize_dark_pool(_flow(strength=20, num_prints=200))
        assert out["signal"] == "WEAK"

    def test_none_when_neutral(self):
        out = summarize_dark_pool(_flow(direction="NEUTRAL", strength=0, buy_ratio=0.5))
        assert out["signal"] == "NONE"

    def test_low_print_count_penalty(self):
        # daily strength 50 (not >50) to suppress the recent-confirm +15 bonus,
        # isolating the low-print penalty.
        daily = [{"flow_direction": "ACCUMULATION", "flow_strength": 50}]
        out = summarize_dark_pool(_flow(strength=60, num_prints=40, daily=daily))
        assert out["score"] == 60 - 20  # -20 for <50 prints

    def test_mid_print_count_penalty(self):
        daily = [{"flow_direction": "ACCUMULATION", "flow_strength": 50}]
        out = summarize_dark_pool(_flow(strength=60, num_prints=75, daily=daily))
        assert out["score"] == 60 - 10  # -10 for 50..100

    def test_sustained_days_bonus(self):
        daily = [{"flow_direction": "ACCUMULATION", "flow_strength": 60}] * 5
        out = summarize_dark_pool(_flow(strength=60, num_prints=200, daily=daily))
        # +20 for sustained >=2, +20 for sustained >=4
        assert out["score"] >= 60 + 40
        assert out["sustained_days"] >= 4


class TestOptionsConflictPenalty:
    """The load-bearing bug fix: penalty must actually fire on all 5 bias states.

    Before the fix:
    - scanner.py read `combined_bias` but fetch_flow emitted `bias` -> penalty NEVER fired
    - even if renamed, bias_map only covered BULLISH/LEAN_BULLISH/BEARISH/LEAN_BEARISH
      while fetch_flow emits STRONGLY_BULLISH/BULLISH/NEUTRAL/BEARISH/STRONGLY_BEARISH
      -> STRONGLY_* cases still wouldn't fire
    """

    def test_penalty_fires_on_bullish_vs_distribution(self):
        baseline = summarize_dark_pool(_flow(direction="DISTRIBUTION", buy_ratio=0.2, strength=60, num_prints=500))
        with_conflict = summarize_dark_pool(
            _flow(
                direction="DISTRIBUTION",
                buy_ratio=0.2,
                strength=60,
                num_prints=500,
                options_bias="BULLISH",
            )
        )
        assert with_conflict["score"] == baseline["score"] - 25
        assert with_conflict["options_conflict"] is True

    def test_penalty_fires_on_strongly_bullish_vs_distribution(self):
        """Previously dead: STRONGLY_BULLISH wasn't in bias_map."""
        baseline = summarize_dark_pool(_flow(direction="DISTRIBUTION", buy_ratio=0.2, strength=60, num_prints=500))
        with_conflict = summarize_dark_pool(
            _flow(
                direction="DISTRIBUTION",
                buy_ratio=0.2,
                strength=60,
                num_prints=500,
                options_bias="STRONGLY_BULLISH",
            )
        )
        assert with_conflict["score"] == baseline["score"] - 25
        assert with_conflict["options_conflict"] is True

    def test_penalty_fires_on_strongly_bearish_vs_accumulation(self):
        baseline = summarize_dark_pool(_flow(strength=60, num_prints=500))
        with_conflict = summarize_dark_pool(
            _flow(
                strength=60,
                num_prints=500,
                options_bias="STRONGLY_BEARISH",
            )
        )
        assert with_conflict["score"] == baseline["score"] - 25
        assert with_conflict["options_conflict"] is True

    def test_no_penalty_when_aligned(self):
        baseline = summarize_dark_pool(_flow(strength=60, num_prints=500))
        aligned = summarize_dark_pool(_flow(strength=60, num_prints=500, options_bias="BULLISH"))
        assert aligned["score"] == baseline["score"]
        assert aligned["options_conflict"] is False

    def test_no_penalty_when_options_neutral(self):
        baseline = summarize_dark_pool(_flow(strength=60, num_prints=500))
        neutral = summarize_dark_pool(_flow(strength=60, num_prints=500, options_bias="NEUTRAL"))
        assert neutral["score"] == baseline["score"]
        assert neutral["options_conflict"] is False


class TestOptionsFlowSummary:
    def test_no_alerts(self):
        out = summarize_options_flow([])
        assert out["bias"] == "NO_DATA"
        assert out["total_alerts"] == 0

    def test_strongly_bullish(self):
        alerts = [{"premium": 100_000, "is_call": True}] * 10
        out = summarize_options_flow(alerts + [{"premium": 10_000, "is_call": False}])
        assert out["bias"] == "STRONGLY_BULLISH"

    def test_strongly_bearish(self):
        alerts = [{"premium": 100_000, "is_call": False}] * 10
        out = summarize_options_flow(alerts + [{"premium": 10_000, "is_call": True}])
        assert out["bias"] == "STRONGLY_BEARISH"

    def test_neutral(self):
        alerts = [
            {"premium": 100_000, "is_call": True},
            {"premium": 100_000, "is_call": False},
        ]
        out = summarize_options_flow(alerts)
        assert out["bias"] == "NEUTRAL"


class TestErrorPassthrough:
    def test_error_passthrough(self):
        out = summarize_dark_pool({"error": "boom"})
        assert out["signal"] == "ERROR"
        assert out["score"] == -1
