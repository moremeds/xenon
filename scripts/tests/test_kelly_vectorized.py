"""Tests for vectorized Kelly batch sizing."""
import numpy as np
import pytest
from scripts.kelly import kelly, kelly_size_batch


class TestKellySizeBatch:
    """Vectorized kelly_size_batch must match scalar kelly_size for every element."""

    def test_batch_matches_scalar_n100(self):
        """Batch results match individual kelly() calls for N=100 random inputs."""
        rng = np.random.default_rng(42)
        n = 100
        prob_wins = rng.uniform(0.0, 1.0, n)
        odds = rng.uniform(0.0, 10.0, n)
        bankroll = 100_000.0
        fraction = 0.25
        max_pct = 0.025

        batch = kelly_size_batch(prob_wins, odds, bankroll, fraction, max_pct)
        assert batch.shape == (n,)

        for i in range(n):
            result = kelly(float(prob_wins[i]), float(odds[i]), fraction)
            if not result["edge_exists"] or odds[i] <= 0:
                expected = 0.0
            else:
                dollar = bankroll * result["fractional_kelly_pct"] / 100.0
                expected = min(dollar, bankroll * max_pct)
            np.testing.assert_allclose(batch[i], expected, atol=1e-10,
                                       err_msg=f"Mismatch at index {i}")

    def test_odds_zero_gives_zero(self):
        """odds <= 0 must produce 0 size."""
        prob_wins = np.array([0.6, 0.7, 0.8])
        odds = np.array([0.0, -1.0, -5.0])
        result = kelly_size_batch(prob_wins, odds, 100_000.0)
        np.testing.assert_array_equal(result, np.zeros(3))

    def test_prob_win_zero(self):
        """prob_win=0 → full_kelly negative → 0 size."""
        prob_wins = np.array([0.0, 0.0])
        odds = np.array([2.0, 5.0])
        result = kelly_size_batch(prob_wins, odds, 100_000.0)
        np.testing.assert_array_equal(result, np.zeros(2))

    def test_prob_win_one(self):
        """prob_win=1 → full_kelly=1.0 → fractional kelly * bankroll, capped."""
        bankroll = 100_000.0
        prob_wins = np.array([1.0])
        odds = np.array([3.0])
        fraction = 0.25
        max_pct = 0.025

        result = kelly_size_batch(prob_wins, odds, bankroll, fraction, max_pct)
        # full_kelly = 1.0 - (0/3) = 1.0, frac = 0.25, dollar = 25000
        # cap = 100000 * 0.025 = 2500
        assert result[0] == pytest.approx(2500.0)

    def test_hard_cap_enforcement(self):
        """Dollar size exceeding bankroll * max_pct gets capped."""
        bankroll = 50_000.0
        max_pct = 0.025  # cap = 1250
        prob_wins = np.array([0.9])
        odds = np.array([5.0])
        fraction = 0.25

        result = kelly_size_batch(prob_wins, odds, bankroll, fraction, max_pct)
        assert result[0] <= bankroll * max_pct + 1e-10

    def test_empty_array(self):
        """Empty input arrays return empty output."""
        result = kelly_size_batch(np.array([]), np.array([]), 100_000.0)
        assert result.shape == (0,)

    def test_single_element_matches_scalar(self):
        """Single-element array matches scalar version exactly."""
        prob = 0.55
        odd = 2.5
        bankroll = 80_000.0
        fraction = 0.25
        max_pct = 0.025

        batch = kelly_size_batch(np.array([prob]), np.array([odd]),
                                 bankroll, fraction, max_pct)

        scalar = kelly(prob, odd, fraction)
        if scalar["edge_exists"]:
            dollar = bankroll * scalar["fractional_kelly_pct"] / 100.0
            expected = min(dollar, bankroll * max_pct)
        else:
            expected = 0.0

        assert batch[0] == pytest.approx(expected, abs=1e-10)
