"""Vectorized portfolio Greeks calculation using NumPy.

Mirrors the approxDelta() function from web/lib/exposureBreakdown.ts exactly.
"""
import numpy as np


def approx_delta_vectorized(
    spots: np.ndarray,
    strikes: np.ndarray,
    dtes: np.ndarray,
    is_call: np.ndarray,
) -> np.ndarray:
    """Vectorized approximate delta matching TypeScript approxDelta().

    For each element:
      if spot <= 0 or strike <= 0 or dte <= 0: return 0.5 (call) or -0.5 (put)
      moneyness = (spot - strike) / strike  [call]  or  (strike - spot) / strike  [put]
      timeFactor = max(0.1, sqrt(dte / 365))
      adjusted = moneyness / (0.2 * timeFactor)
      callDelta = 0.5 + 0.5 * tanh(adjusted * 2)
      return callDelta [call] or callDelta - 1 [put]
    """
    spots = np.asarray(spots, dtype=np.float64)
    strikes = np.asarray(strikes, dtype=np.float64)
    dtes = np.asarray(dtes, dtype=np.float64)
    is_call = np.asarray(is_call, dtype=bool)

    if len(spots) == 0:
        return np.array([], dtype=np.float64)

    # Fallback mask: any of spot/strike/dte <= 0
    fallback = (spots <= 0) | (strikes <= 0) | (dtes <= 0)

    # Compute moneyness (safe division; fallback elements will be overwritten)
    safe_strikes = np.where(strikes > 0, strikes, 1.0)  # avoid div by zero
    call_moneyness = (spots - strikes) / safe_strikes
    put_moneyness = (strikes - spots) / safe_strikes
    moneyness = np.where(is_call, call_moneyness, put_moneyness)

    # Time factor
    time_factor = np.maximum(0.1, np.sqrt(dtes / 365.0))

    # Adjusted moneyness
    adjusted = moneyness / (0.2 * time_factor)

    # Call delta via tanh
    call_delta = 0.5 + 0.5 * np.tanh(adjusted * 2.0)

    # Put delta = call_delta - 1
    raw_delta = np.where(is_call, call_delta, call_delta - 1.0)

    # Apply fallback for invalid inputs
    fallback_val = np.where(is_call, 0.5, -0.5)
    raw_delta = np.where(fallback, fallback_val, raw_delta)

    return raw_delta


def portfolio_greeks_vectorized(
    spots: np.ndarray,
    strikes: np.ndarray,
    dtes: np.ndarray,
    signs: np.ndarray,
    contracts: np.ndarray,
    is_call: np.ndarray,
) -> dict:
    """Compute portfolio Greeks for N legs simultaneously.

    Args:
        spots: Underlying spot prices per leg
        strikes: Strike prices per leg
        dtes: Days to expiry per leg
        signs: +1 for LONG, -1 for SHORT per leg
        contracts: Number of contracts per leg
        is_call: True for Call, False for Put per leg

    Returns:
        dict with:
            raw_deltas: per-leg raw delta (unsigned by direction)
            leg_deltas: per-leg delta (sign * rawDelta * contracts * 100)
            net_delta: sum of leg_deltas
            dollar_delta: sum of leg_deltas * spots
    """
    spots = np.asarray(spots, dtype=np.float64)
    signs = np.asarray(signs, dtype=np.float64)
    contracts = np.asarray(contracts, dtype=np.float64)

    raw_deltas = approx_delta_vectorized(spots, strikes, dtes, is_call)
    leg_deltas = signs * raw_deltas * contracts * 100.0

    net_delta = float(np.sum(leg_deltas)) if len(leg_deltas) > 0 else 0.0
    dollar_delta = float(np.sum(leg_deltas * spots)) if len(leg_deltas) > 0 else 0.0

    return {
        "raw_deltas": raw_deltas,
        "leg_deltas": leg_deltas,
        "net_delta": net_delta,
        "dollar_delta": dollar_delta,
    }
