"""Extended tests for kelly.py — thresholds and dollar sizing."""
import pytest

from kelly import kelly


class TestKellyRecommendations:
    def test_strong_recommendation(self):
        """full_kelly > 0.10 → STRONG."""
        result = kelly(prob_win=0.7, odds=3.0)
        assert result["full_kelly_pct"] > 10.0
        assert result["recommendation"] == "STRONG"

    def test_marginal_recommendation(self):
        """full_kelly between 0.025 and 0.10 → MARGINAL."""
        # kelly = p - q/odds = 0.55 - 0.45/2 = 0.55 - 0.225 = 0.325 → too high
        # Need: 0.025 < kelly <= 0.10
        # kelly = p - (1-p)/odds → 0.54 - 0.46/3 = 0.54 - 0.153 = 0.387 too high
        # Use p=0.52, odds=1.2 → 0.52 - 0.48/1.2 = 0.52 - 0.4 = 0.12 too high
        # Use p=0.52, odds=1.05 → 0.52 - 0.48/1.05 = 0.52 - 0.457 = 0.063
        result = kelly(prob_win=0.52, odds=1.05)
        fk = result["full_kelly_pct"] / 100
        assert 0.025 < fk <= 0.10
        assert result["recommendation"] == "MARGINAL"

    def test_weak_recommendation(self):
        """full_kelly > 0 but <= 0.025 → WEAK."""
        # kelly = p - (1-p)/odds
        # p=0.51, odds=1.02 → 0.51 - 0.49/1.02 = 0.51 - 0.4804 = 0.0296 → too high still
        # p=0.505, odds=1.01 → 0.505 - 0.495/1.01 = 0.505 - 0.49 = 0.015
        result = kelly(prob_win=0.505, odds=1.01)
        fk = result["full_kelly_pct"] / 100
        assert 0 < fk <= 0.025
        assert result["recommendation"] == "WEAK"

    def test_no_edge_do_not_bet(self):
        result = kelly(prob_win=0.3, odds=1.0)
        assert result["recommendation"] == "DO NOT BET"
        assert result["edge_exists"] is False

    def test_custom_fraction(self):
        """Custom fraction scales fractional kelly."""
        r_quarter = kelly(prob_win=0.6, odds=2.0, fraction=0.25)
        r_half = kelly(prob_win=0.6, odds=2.0, fraction=0.50)
        assert abs(r_half["fractional_kelly_pct"] - r_quarter["fractional_kelly_pct"] * 2) < 0.01


class TestKellyDollarSizing:
    def test_bankroll_sizing(self):
        """Simulate CLI --bankroll logic."""
        result = kelly(prob_win=0.6, odds=2.0, fraction=0.25)
        bankroll = 100000
        dollar_size = bankroll * result["fractional_kelly_pct"] / 100
        max_per_position = bankroll * 0.025
        use_size = min(dollar_size, max_per_position)
        assert dollar_size > 0
        assert use_size <= max_per_position

    def test_zero_odds(self):
        result = kelly(prob_win=0.6, odds=0)
        assert result["fractional_kelly_pct"] == 0
        assert result["edge_exists"] is False

    def test_edge_boundary(self):
        """Exactly break-even edge."""
        # p=0.5, odds=1.0 → kelly = 0.5 - 0.5/1.0 = 0 → no edge
        result = kelly(prob_win=0.5, odds=1.0)
        assert result["edge_exists"] is False
        assert result["recommendation"] == "DO NOT BET"
