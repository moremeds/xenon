"""Tests for discover.py — dark pool day analysis and scoring."""
import pytest
from datetime import datetime

from discover import (
    analyze_darkpool_day,
    calculate_score,
    is_market_open,
    WEIGHTS,
)


# ── is_market_open ──────────────────────────────────────────────────

class TestIsMarketOpen:
    def test_weekday_open(self):
        assert is_market_open(datetime(2026, 3, 3)) is True

    def test_weekend_closed(self):
        assert is_market_open(datetime(2026, 3, 7)) is False

    def test_holiday_closed(self):
        assert is_market_open(datetime(2026, 1, 19)) is False


# ── analyze_darkpool_day ────────────────────────────────────────────

class TestAnalyzeDarkpoolDay:
    def test_empty_trades_no_data(self):
        result = analyze_darkpool_day([])
        assert result["direction"] == "NO_DATA"
        assert result["buy_ratio"] is None
        assert result["prints"] == 0

    def test_strong_buy_accumulation(self):
        trades = [
            {"size": "5000", "price": "101", "nbbo_bid": "99", "nbbo_ask": "101"},
        ]
        result = analyze_darkpool_day(trades)
        assert result["direction"] == "ACCUMULATION"
        assert result["buy_ratio"] == 1.0
        assert result["strength"] == 100.0

    def test_strong_sell_distribution(self):
        trades = [
            {"size": "5000", "price": "99", "nbbo_bid": "99", "nbbo_ask": "101"},
        ]
        result = analyze_darkpool_day(trades)
        assert result["direction"] == "DISTRIBUTION"
        assert result["buy_ratio"] == 0.0

    def test_canceled_skipped(self):
        trades = [
            {"size": "5000", "price": "101", "nbbo_bid": "99", "nbbo_ask": "101",
             "canceled": True},
        ]
        result = analyze_darkpool_day(trades)
        # Canceled trades still counted in prints, but not in volume
        assert result["buy_ratio"] is None

    def test_neutral_balanced(self):
        trades = [
            {"size": "1000", "price": "101", "nbbo_bid": "99", "nbbo_ask": "101"},
            {"size": "1000", "price": "99", "nbbo_bid": "99", "nbbo_ask": "101"},
        ]
        result = analyze_darkpool_day(trades)
        assert result["direction"] == "NEUTRAL"
        assert result["buy_ratio"] == 0.5

    def test_strength_capped_at_100(self):
        trades = [
            {"size": "5000", "price": "110", "nbbo_bid": "99", "nbbo_ask": "101"},
        ]
        result = analyze_darkpool_day(trades)
        assert result["strength"] <= 100.0


# ── calculate_score ─────────────────────────────────────────────────

class TestCalculateScore:
    def test_weights_sum_to_100(self):
        assert sum(WEIGHTS.values()) == 100

    def test_max_score_all_components_high(self):
        result = calculate_score(
            dp_strength=100,
            dp_sustained=5,
            has_confluence=True,
            vol_oi_ratio=5.0,
            sweep_count=3,
            alert_count=10,
        )
        assert result["total"] == 100.0

    def test_zero_score_no_signals(self):
        result = calculate_score(
            dp_strength=0,
            dp_sustained=0,
            has_confluence=False,
            vol_oi_ratio=0.5,
            sweep_count=0,
            alert_count=0,
        )
        assert result["total"] == 0.0

    def test_dp_sustained_scaling(self):
        """1 day = 20, 5 days = 100."""
        r1 = calculate_score(0, 1, False, 0, 0, 0)
        r5 = calculate_score(0, 5, False, 0, 0, 0)
        assert r1["components"]["dp_sustained"] == 20
        assert r5["components"]["dp_sustained"] == 100

    def test_confluence_binary(self):
        r_yes = calculate_score(0, 0, True, 0, 0, 0)
        r_no = calculate_score(0, 0, False, 0, 0, 0)
        assert r_yes["components"]["confluence"] == 100
        assert r_no["components"]["confluence"] == 0

    def test_vol_oi_normalization(self):
        """vol_oi <= 1 → 0, 2 → 50, 4+ → 100."""
        r_low = calculate_score(0, 0, False, 0.5, 0, 0)
        r_mid = calculate_score(0, 0, False, 2.0, 0, 0)
        r_high = calculate_score(0, 0, False, 5.0, 0, 0)
        assert r_low["components"]["vol_oi"] == 0
        assert r_mid["components"]["vol_oi"] == 50
        assert r_high["components"]["vol_oi"] == 100

    def test_sweep_count_scaling(self):
        """0 → 0, 1 → 50, 2+ → 100."""
        r0 = calculate_score(0, 0, False, 0, 0, 0)
        r1 = calculate_score(0, 0, False, 0, 1, 0)
        r2 = calculate_score(0, 0, False, 0, 2, 0)
        assert r0["components"]["sweeps"] == 0
        assert r1["components"]["sweeps"] == 50
        assert r2["components"]["sweeps"] == 100

    def test_weighted_breakdown_matches_total(self):
        result = calculate_score(
            dp_strength=60,
            dp_sustained=3,
            has_confluence=True,
            vol_oi_ratio=2.5,
            sweep_count=1,
            alert_count=5,
        )
        expected_total = sum(result["weighted"].values())
        assert abs(result["total"] - expected_total) < 0.2
