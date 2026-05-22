"""Tests for flow_analysis.classify_portfolio.

Covers alignment logic (supports / against / mixed / non_directional /
neutral) and the NVDA repro case that motivated the rewrite.
"""

from __future__ import annotations

import asyncio

from xenon.api.services.flow_analysis import (
    align,
    classify_portfolio,
)

from xenon.utils.portfolio_adapter import LoadResult, NormalizedPosition


def _pos(ticker, structure_type, structure=None, direction="LONG", extra_raw=None):
    raw = {"structure_type": structure_type, "direction": direction}
    if extra_raw:
        raw.update(extra_raw)
    return NormalizedPosition(
        ticker=ticker,
        direction=direction,
        structure=structure or structure_type,
        qty=1,
        raw=raw,
    )


def _summary(direction="ACCUMULATION", strength=60.0, buy_ratio=0.78, signal="STRONG"):
    return {
        "direction": direction,
        "strength": strength,
        "buy_ratio": buy_ratio,
        "signal": signal,
        "score": strength,
        "options_conflict": False,
    }


# ── align() unit tests ───────────────────────────────────────────────────
class TestAlign:
    def test_non_directional_bias_bypasses_verdict(self):
        assert align("income", _summary(), {"bias": "STRONGLY_BULLISH"}) == "non_directional"
        assert align("hedge", _summary(), {"bias": "NEUTRAL"}) == "non_directional"
        assert align("neutral_vol", _summary(), {"bias": "BEARISH"}) == "non_directional"
        assert align("unknown", _summary(), {"bias": "BULLISH"}) == "non_directional"

    def test_supports_when_both_agree_bullish(self):
        assert align("bullish", _summary(direction="ACCUMULATION"), {"bias": "BULLISH"}) == "supports"

    def test_against_when_both_contradict(self):
        assert align("bearish", _summary(direction="ACCUMULATION"), {"bias": "BULLISH"}) == "against"

    def test_mixed_when_dp_and_options_disagree_about_direction(self):
        # DP bullish, options bearish → mixed for either position bias
        assert align("bullish", _summary(direction="ACCUMULATION"), {"bias": "BEARISH"}) == "mixed"
        assert align("bearish", _summary(direction="ACCUMULATION"), {"bias": "BEARISH"}) == "mixed"

    def test_neutral_when_both_silent(self):
        assert (
            align("bullish", _summary(direction="NEUTRAL", strength=0, signal="NONE"), {"bias": "NEUTRAL"}) == "neutral"
        )

    def test_weak_dp_alone_agrees_degrades_to_mixed(self):
        """One silent, one agrees → mixed (the resurrected `watch` case)."""
        assert (
            align("bullish", _summary(direction="ACCUMULATION", signal="WEAK", strength=20), {"bias": "NEUTRAL"})
            == "mixed"
        )


# ── NVDA repro: the whole point of this rewrite ──────────────────────────
class TestNvdaRepro:
    """Real-world case from the user's report. All three are on NVDA."""

    def _entry(self):
        return {
            "ticker": "NVDA",
            "dark_pool_summary": _summary(direction="ACCUMULATION", strength=57.8, buy_ratio=0.789),
            "options_flow_summary": {"bias": "BULLISH"},
        }

    def test_long_call_supports(self):
        pos = _pos("NVDA", "Long Call", "Long Call $185.0")
        entry = self._entry()
        verdict = align("bullish", entry["dark_pool_summary"], entry["options_flow_summary"])
        assert verdict == "supports"

    def test_long_put_goes_against_not_supports(self):
        """The headline bug: was in supports, must now be in against."""
        verdict = align("bearish", self._entry()["dark_pool_summary"], self._entry()["options_flow_summary"])
        assert verdict == "against"

    def test_short_put_supports_not_against(self):
        """Second symptom: was in against, must now be in supports."""
        verdict = align("bullish", self._entry()["dark_pool_summary"], self._entry()["options_flow_summary"])
        assert verdict == "supports"


# ── classify_portfolio integration ────────────────────────────────────────
class FakeCache:
    """Minimal flow-analysis cache fake for the classifier. Supplies entries
    synchronously via get_entry; get_or_run is invoked only on miss."""

    def __init__(self, entries: dict, misses: set[str] = None):
        self._entries = entries
        self.misses = misses or set()
        self.get_or_run_calls: list[str] = []

    def get_entry(self, ticker: str):
        return self._entries.get(ticker)

    async def get_or_run(self, ticker: str, *, runner, force=False, sources=None):
        self.get_or_run_calls.append(ticker)
        # Populate from the miss fixture
        if ticker in self.misses:
            entry = {
                "dark_pool_summary": _summary(direction="ACCUMULATION", strength=70),
                "options_flow_summary": {"bias": "BULLISH"},
            }
            self._entries[ticker] = entry
            return entry, True
        return self._entries.get(ticker, {}), False


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestClassifyPortfolio:
    def test_nvda_three_positions_correctly_bucketed(self):
        positions = [
            _pos("NVDA", "Long Call", "Long Call $185.0", direction="LONG"),
            _pos("NVDA", "Long Put", "Long Put $160.0", direction="LONG"),
            _pos("NVDA", "Short Put", "Short Put $170.0", direction="SHORT"),
        ]
        cache = FakeCache(
            entries={
                "NVDA": {
                    "dark_pool_summary": _summary(direction="ACCUMULATION", strength=57.8, buy_ratio=0.789),
                    "options_flow_summary": {"bias": "BULLISH"},
                }
            }
        )
        load_result = LoadResult(positions=positions, skipped_unsupported=0)

        out = asyncio.run(
            classify_portfolio(
                account="futu",
                cache=cache,
                load_positions=lambda _acct: load_result,
                runner=None,
            )
        )

        supports_tickers = [(r["ticker"], r["structure"]) for r in out["supports"]]
        against_tickers = [(r["ticker"], r["structure"]) for r in out["against"]]

        # Long Call → supports
        assert ("NVDA", "Long Call $185.0") in supports_tickers
        # Short Put → supports (was previously against — THE BUG FIX)
        assert ("NVDA", "Short Put $170.0") in supports_tickers
        # Long Put → against (was previously supports — THE BUG FIX)
        assert ("NVDA", "Long Put $160.0") in against_tickers
