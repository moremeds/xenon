"""Tests for scanner_lib base models."""

from __future__ import annotations

import pytest


def test_base_signal_hit_creation():
    from scripts.scanner_lib.models import BaseSignalHit

    hit = BaseSignalHit(ticker="AAPL", signal_type="trend_ma", score=0.85, evidence={"ma_20": 185.0, "ma_50": 180.0})
    assert hit.ticker == "AAPL"
    assert hit.signal_type == "trend_ma"
    assert hit.score == 0.85
    assert hit.evidence["ma_20"] == 185.0


def test_base_signal_hit_is_frozen():
    from scripts.scanner_lib.models import BaseSignalHit

    hit = BaseSignalHit(ticker="AAPL", signal_type="trend", score=0.5, evidence={})
    with pytest.raises(AttributeError):
        hit.score = 0.9


def test_base_signal_hit_score_bounds():
    from scripts.scanner_lib.models import BaseSignalHit

    with pytest.raises(ValueError, match="score must be between 0 and 1"):
        BaseSignalHit(ticker="AAPL", signal_type="trend", score=1.5, evidence={})
    with pytest.raises(ValueError, match="score must be between 0 and 1"):
        BaseSignalHit(ticker="AAPL", signal_type="trend", score=-0.1, evidence={})


def test_base_context_flag_creation():
    from scripts.scanner_lib.models import BaseContextFlag

    flag = BaseContextFlag(ticker="AAPL", layer="news", label="earnings_soon", value=7.0)
    assert flag.ticker == "AAPL"
    assert flag.layer == "news"
    assert flag.label == "earnings_soon"
    assert flag.value == 7.0


def test_base_scan_candidate_creation():
    from scripts.scanner_lib.models import BaseScanCandidate

    c = BaseScanCandidate(
        ticker="NVDA", direction="bullish", final_score=0.82, scores={"trend": 0.91, "structure": 0.75}
    )
    assert c.ticker == "NVDA"
    assert c.direction == "bullish"
    assert c.final_score == 0.82
    assert c.scores["trend"] == 0.91


def test_base_scan_candidate_default_fields():
    from scripts.scanner_lib.models import BaseScanCandidate

    c = BaseScanCandidate(ticker="AAPL", direction="bearish", final_score=0.5, scores={})
    assert c.flags == []
    assert c.summaries == {}
