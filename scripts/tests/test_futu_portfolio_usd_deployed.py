"""Futu fetch_portfolio: gross_position_value (the UI's OPEN RISK / deployed
capital) must be USD-denominated.

Per-position market_value is in each security's NATIVE currency, so summing
across rows mixes units. A single JPY row (¥2,450,000 ≈ $15k) was being added
raw into the deployed total, inflating "open risk" to $2.9M. The account query
runs with currency=USD, so its long_mv/short_mv are already USD — those are the
correct source for the aggregate.

Real 2026-06-22 IB+FUTU snapshot (data/futu_portfolio.json):
  account_raw.long_mv  =  352106.1986  (USD)
  account_raw.short_mv = -116898.27    (USD)
  gross = long + |short| = 469004.4686 (USD)  ← correct open risk
  sum(|market_value| native) = 2903848.71     ← the buggy ¥-inflated figure
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from xenon.clients.futu_client import FutuClient

# Real account aggregates from the live snapshot (queried currency=USD).
LONG_MV_USD = 352106.1986
SHORT_MV_USD = -116898.27
GROSS_USD = LONG_MV_USD + abs(SHORT_MV_USD)  # 469004.4686


def _positions_env(positions):
    return {
        "fetched_at": "2026-06-22T14:00:00Z",
        "data_as_of": "2026-06-22T14:00:00Z",
        "account_id": "123",
        "is_stale": False,
        "warnings": [],
        "positions": positions,
    }


def _account(long_mv, short_mv):
    return {
        "net_liquidation": 207348.111,
        "maintenance_margin": 139881.42,
        "cash": -27859.82,
        "buying_power": 50000.0,
        "initial_margin": 100000.0,
        "available_funds": 50000.0,
        "long_mv": long_mv,
        "short_mv": short_mv,
        "is_stale": False,
    }


# A USD row plus the real ¥2,450,000 JPY row that triggered the inflation.
_POSITIONS = [
    {
        "futu_code": "US.TSLA260821P400000",
        "normalized": {"kind": "OPT", "symbol": "TSLA"},
        "quantity": -1,
        "avg_cost": 250.0,
        "market_price": 282.5,
        "market_value": -28250.0,
        "unrealized_pnl": 0.0,
        "unrealized_pnl_pct": 0.0,
        "currency": "USD",
        "position_side": "SHORT",
    },
    {
        "futu_code": "JP.5016",
        "normalized": {"kind": "STK", "symbol": "5016"},
        "quantity": 100,
        "avg_cost": 24500.0,
        "market_price": 24500.0,
        "market_value": 2_450_000.0,  # ¥ — must NOT be summed into a USD total
        "unrealized_pnl": 0.0,
        "unrealized_pnl_pct": 0.0,
        "currency": "JPY",
        "position_side": "LONG",
    },
]


def test_gross_position_value_uses_usd_account_aggregates():
    """gross = long_mv + |short_mv| (USD), not the ¥-inflated native sum."""
    c = FutuClient(trd_env="REAL")
    c.fetch_positions = MagicMock(return_value=_positions_env(_POSITIONS))
    c.fetch_account = MagicMock(return_value=_account(LONG_MV_USD, SHORT_MV_USD))

    env = c.fetch_portfolio()
    gross = env["account_summary"]["gross_position_value"]

    assert gross == pytest.approx(GROSS_USD, abs=0.01)
    # The ¥2.45M native magnitude must be nowhere near the deployed total.
    assert gross < 1_000_000


def test_gross_position_value_falls_back_to_native_sum_when_account_aggregates_absent():
    """Older snapshots / accounts without long_mv/short_mv keep the native-sum
    behavior (best available) rather than reporting zero."""
    c = FutuClient(trd_env="REAL")
    c.fetch_positions = MagicMock(return_value=_positions_env(_POSITIONS))
    c.fetch_account = MagicMock(return_value=_account(None, None))

    env = c.fetch_portfolio()
    gross = env["account_summary"]["gross_position_value"]

    # Fallback = sum(|market_value|) = 28250 + 2_450_000.
    assert gross == pytest.approx(28250.0 + 2_450_000.0, abs=0.01)
