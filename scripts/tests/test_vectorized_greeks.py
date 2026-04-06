"""Tests for vectorized portfolio Greeks calculation."""
import math
import numpy as np
import pytest
from scripts.utils.vectorized_greeks import portfolio_greeks_vectorized


def ts_approx_delta(spot: float, strike: float, dte: float, opt_type: str) -> float:
    """Python mirror of the TypeScript approxDelta() for cross-validation."""
    if spot <= 0 or strike <= 0 or dte <= 0:
        return 0.5 if opt_type == "Call" else -0.5
    if opt_type == "Call":
        moneyness = (spot - strike) / strike
    else:
        moneyness = (strike - spot) / strike
    time_factor = max(0.1, math.sqrt(dte / 365))
    adjusted = moneyness / (0.2 * time_factor)
    call_delta = 0.5 + 0.5 * math.tanh(adjusted * 2)
    return call_delta if opt_type == "Call" else call_delta - 1


class TestPortfolioGreeksVectorized:

    def test_long_call_delta_positive(self):
        """Long call must have positive raw delta."""
        result = portfolio_greeks_vectorized(
            spots=np.array([100.0]),
            strikes=np.array([100.0]),
            dtes=np.array([30.0]),
            signs=np.array([1.0]),
            contracts=np.array([1.0]),
            is_call=np.array([True]),
        )
        assert result["raw_deltas"][0] > 0

    def test_short_call_delta_negative(self):
        """Short call must have negative raw delta (sign * delta)."""
        result = portfolio_greeks_vectorized(
            spots=np.array([100.0]),
            strikes=np.array([100.0]),
            dtes=np.array([30.0]),
            signs=np.array([-1.0]),
            contracts=np.array([1.0]),
            is_call=np.array([True]),
        )
        assert result["raw_deltas"][0] > 0  # raw delta itself is positive
        assert result["leg_deltas"][0] < 0  # sign-adjusted is negative

    def test_long_put_delta_negative(self):
        """Long put must have negative raw delta."""
        result = portfolio_greeks_vectorized(
            spots=np.array([100.0]),
            strikes=np.array([100.0]),
            dtes=np.array([30.0]),
            signs=np.array([1.0]),
            contracts=np.array([1.0]),
            is_call=np.array([False]),
        )
        assert result["raw_deltas"][0] < 0

    def test_short_put_delta_positive(self):
        """Short put: raw delta negative, sign=-1 → leg_delta positive."""
        result = portfolio_greeks_vectorized(
            spots=np.array([100.0]),
            strikes=np.array([100.0]),
            dtes=np.array([30.0]),
            signs=np.array([-1.0]),
            contracts=np.array([1.0]),
            is_call=np.array([False]),
        )
        assert result["raw_deltas"][0] < 0
        assert result["leg_deltas"][0] > 0

    def test_net_delta_sums_correctly(self):
        """Net delta = sum of all leg deltas."""
        result = portfolio_greeks_vectorized(
            spots=np.array([150.0, 150.0]),
            strikes=np.array([145.0, 155.0]),
            dtes=np.array([45.0, 45.0]),
            signs=np.array([1.0, -1.0]),
            contracts=np.array([5.0, 5.0]),
            is_call=np.array([True, True]),
        )
        expected_net = float(np.sum(result["leg_deltas"]))
        assert result["net_delta"] == pytest.approx(expected_net)

    def test_dollar_delta(self):
        """Dollar delta = sum(leg_delta * spot)."""
        spots = np.array([200.0, 200.0])
        result = portfolio_greeks_vectorized(
            spots=spots,
            strikes=np.array([190.0, 210.0]),
            dtes=np.array([30.0, 30.0]),
            signs=np.array([1.0, -1.0]),
            contracts=np.array([2.0, 2.0]),
            is_call=np.array([True, True]),
        )
        expected_dd = float(np.sum(result["leg_deltas"] * spots))
        assert result["dollar_delta"] == pytest.approx(expected_dd)

    def test_edge_case_spot_zero(self):
        """spot=0 → fallback delta (0.5 for call, -0.5 for put)."""
        result = portfolio_greeks_vectorized(
            spots=np.array([0.0]),
            strikes=np.array([100.0]),
            dtes=np.array([30.0]),
            signs=np.array([1.0]),
            contracts=np.array([1.0]),
            is_call=np.array([True]),
        )
        assert result["raw_deltas"][0] == pytest.approx(0.5)

    def test_edge_case_strike_zero(self):
        """strike=0 → fallback delta."""
        result = portfolio_greeks_vectorized(
            spots=np.array([100.0]),
            strikes=np.array([0.0]),
            dtes=np.array([30.0]),
            signs=np.array([1.0]),
            contracts=np.array([False]),
            is_call=np.array([False]),
        )
        assert result["raw_deltas"][0] == pytest.approx(-0.5)

    def test_edge_case_dte_zero(self):
        """dte=0 → fallback delta."""
        result = portfolio_greeks_vectorized(
            spots=np.array([100.0]),
            strikes=np.array([100.0]),
            dtes=np.array([0.0]),
            signs=np.array([1.0]),
            contracts=np.array([1.0]),
            is_call=np.array([True]),
        )
        assert result["raw_deltas"][0] == pytest.approx(0.5)

    def test_cross_validate_with_typescript(self):
        """Results must match the TypeScript approxDelta() for known inputs."""
        test_cases = [
            (150.0, 140.0, 30.0, True),   # ITM call
            (150.0, 160.0, 30.0, True),   # OTM call
            (150.0, 150.0, 90.0, True),   # ATM call, longer DTE
            (150.0, 140.0, 30.0, False),  # OTM put
            (150.0, 160.0, 30.0, False),  # ITM put
            (50.0, 50.0, 7.0, True),      # ATM call, short DTE
            (300.0, 250.0, 180.0, True),  # deep ITM call
            (300.0, 350.0, 180.0, False), # deep ITM put
        ]
        spots = np.array([c[0] for c in test_cases])
        strikes = np.array([c[1] for c in test_cases])
        dtes = np.array([c[2] for c in test_cases])
        is_call = np.array([c[3] for c in test_cases])
        signs = np.ones(len(test_cases))
        contracts = np.ones(len(test_cases))

        result = portfolio_greeks_vectorized(spots, strikes, dtes, signs,
                                             contracts, is_call)

        for i, (sp, st, dt, ic) in enumerate(test_cases):
            opt_type = "Call" if ic else "Put"
            expected = ts_approx_delta(sp, st, dt, opt_type)
            np.testing.assert_allclose(
                result["raw_deltas"][i], expected, atol=1e-12,
                err_msg=f"Mismatch for {opt_type} spot={sp} strike={st} dte={dt}"
            )

    def test_empty_arrays(self):
        """Empty input returns empty arrays and zero aggregates."""
        result = portfolio_greeks_vectorized(
            spots=np.array([]),
            strikes=np.array([]),
            dtes=np.array([]),
            signs=np.array([]),
            contracts=np.array([]),
            is_call=np.array([], dtype=bool),
        )
        assert result["raw_deltas"].shape == (0,)
        assert result["leg_deltas"].shape == (0,)
        assert result["net_delta"] == 0.0
        assert result["dollar_delta"] == 0.0
