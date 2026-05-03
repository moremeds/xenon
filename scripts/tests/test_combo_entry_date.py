"""Entry-date resolution scenarios for ib_sync.convert_to_portfolio_format.

Post-PG-cutoff: the function reads from PG via load_entry_date_lookups_sync
instead of reading data/blotter.json + trade_log.json + portfolio.json.
These tests mock the helper to exercise the same scenarios the JSON-era tests
covered.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from xenon.execution import ib_sync
from xenon.utils.portfolio_loader import EntryDateLookups


def _empty_lookups() -> EntryDateLookups:
    return EntryDateLookups(
        per_contract_dates={},
        per_ticker_dates={},
        trade_log_dates={},
        prev_portfolio_dates={},
    )


def _patch_lookups(lookups: EntryDateLookups):
    """Patch the loader at its source module so the lazy import inside
    convert_to_portfolio_format picks up our fixture.
    """
    return patch("xenon.utils.portfolio_loader.load_entry_date_lookups_sync", return_value=lookups)


def _risk_reversal_position() -> list[dict]:
    return [
        {
            "id": 16,
            "ticker": "PLTR",
            "structure": "Risk Reversal (P$152.5/C$155.0)",
            "structure_type": "Risk Reversal",
            "risk_profile": "undefined",
            "expiry": "2026-03-27",
            "contracts": 20,
            "direction": "COMBO",
            "entry_cost": -1571.92,
            "max_risk": None,
            "market_value": -1760.0,
            "market_price_is_calculated": False,
            "ib_daily_pnl": None,
            "legs": [
                {
                    "direction": "LONG",
                    "contracts": 20,
                    "type": "Call",
                    "strike": 155.0,
                    "entry_cost": 0,
                    "avg_cost": 0,
                    "market_price": 0,
                    "market_value": 0,
                    "market_price_is_calculated": False,
                },
                {
                    "direction": "SHORT",
                    "contracts": 20,
                    "type": "Put",
                    "strike": 152.5,
                    "entry_cost": 0,
                    "avg_cost": 0,
                    "market_price": 0,
                    "market_value": 0,
                    "market_price_is_calculated": False,
                },
            ],
            "kelly_optimal": None,
            "target": None,
            "stop": None,
        }
    ]


def _aaoi_long_put_position() -> list[dict]:
    return [
        {
            "id": 1,
            "ticker": "AAOI",
            "structure": "Long Put $110.0",
            "structure_type": "Long Put",
            "risk_profile": "defined",
            "expiry": "2026-04-02",
            "contracts": 25,
            "direction": "LONG",
            "entry_cost": 19367.51,
            "max_risk": None,
            "market_value": 18000.0,
            "market_price_is_calculated": False,
            "ib_daily_pnl": None,
            "legs": [
                {
                    "direction": "LONG",
                    "contracts": 25,
                    "type": "Put",
                    "strike": 110.0,
                    "entry_cost": 19367.51,
                    "avg_cost": 774.70,
                    "market_price": 7.20,
                    "market_value": 18000.0,
                    "market_price_is_calculated": False,
                },
            ],
            "kelly_optimal": None,
            "target": None,
            "stop": None,
        }
    ]


class TestComboEntryDateResolution(unittest.TestCase):
    def test_multi_leg_uses_per_contract_fills_min(self):
        """When PG order_fills has per-contract dates for both legs, take the
        min. Mirrors the old per-contract blotter behavior.
        """
        lookups = EntryDateLookups(
            per_contract_dates={
                "PLTR|2026-03-27|C|155.0": "2026-03-24",
                "PLTR|2026-03-27|P|152.5": "2026-03-24",
            },
            per_ticker_dates={"PLTR": "2026-03-19"},
            trade_log_dates={},
            prev_portfolio_dates={},
        )
        with _patch_lookups(lookups):
            result = ib_sync.convert_to_portfolio_format(
                {"NetLiquidation": 1_000_000},
                _risk_reversal_position(),
                {},
            )
        self.assertEqual(result["positions"][0]["entry_date"], "2026-03-24")

    def test_fill_dates_resolve_when_pg_lookups_empty(self):
        """In-session fill_dates (passed by ib_sync, not PG) win when PG lookups
        have no per-contract or trade_log entry — same-session new trades.
        """
        with _patch_lookups(_empty_lookups()):
            result = ib_sync.convert_to_portfolio_format(
                {"NetLiquidation": 1_000_000},
                _aaoi_long_put_position(),
                {},
                fill_dates={"AAOI|2026-04-02|P|110.0": "2026-03-25"},
            )
        self.assertEqual(result["positions"][0]["entry_date"], "2026-03-25")

    def test_per_contract_pg_dates_beat_session_fill_dates(self):
        """Per-contract PG dates outrank in-session fill_dates (older PG date
        is the truthful entry, fill_dates only fills the same-session gap).
        """
        lookups = EntryDateLookups(
            per_contract_dates={"AAOI|2026-04-02|P|110.0": "2026-03-24"},
            per_ticker_dates={"AAOI": "2026-03-24"},
            trade_log_dates={},
            prev_portfolio_dates={},
        )
        with _patch_lookups(lookups):
            result = ib_sync.convert_to_portfolio_format(
                {"NetLiquidation": 1_000_000},
                _aaoi_long_put_position(),
                {},
                fill_dates={"AAOI|2026-04-02|P|110.0": "2026-03-25"},
            )
        self.assertEqual(result["positions"][0]["entry_date"], "2026-03-24")

    def test_unknown_when_no_source_resolves(self):
        """No PG data, no fill_dates → 'unknown'."""
        with _patch_lookups(_empty_lookups()):
            result = ib_sync.convert_to_portfolio_format(
                {"NetLiquidation": 1_000_000},
                _aaoi_long_put_position(),
                {},
            )
        self.assertEqual(result["positions"][0]["entry_date"], "unknown")


if __name__ == "__main__":
    unittest.main()
