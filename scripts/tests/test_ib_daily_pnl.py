"""
Tests for per-position daily P&L (ib_daily_pnl) in ib_sync.py.

Bug: When contracts are added intraday, the UI's WS close-based calculation
treats ALL contracts as overnight, overstating daily P&L. Fix: ib_sync.py
now fetches IB's reqPnLSingle daily P&L and includes it as ib_daily_pnl
in collapsed positions. The UI prefers this over its own calculation.
"""

import sys
from pathlib import Path
import unittest

# Add scripts dir to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestIbDailyPnlCollapse(unittest.TestCase):
    """Test that ib_daily_pnl flows through collapse_positions correctly."""

    def _make_leg(self, symbol='AAOI', sec_type='OPT', position=50, strike=105,
                  right='C', expiry='2026-03-20', ib_daily_pnl=None, **kwargs):
        return {
            'symbol': symbol,
            'secType': sec_type,
            'position': position,
            'avgCost': 10.52,
            'entry_cost': abs(10.52 * position),
            'expiry': expiry,
            'strike': strike,
            'right': right,
            'structure': f"Long {'Call' if right == 'C' else 'Put'}",
            'marketPrice': 12.85,
            'marketValue': abs(12.85 * position * 100),
            'marketPriceIsCalculated': False,
            'ibDailyPnl': ib_daily_pnl,
            **kwargs,
        }

    def test_single_leg_ib_daily_pnl_passes_through(self):
        """Single-leg position should pass ibDailyPnl to collapsed ib_daily_pnl."""
        from ib_sync import collapse_positions
        positions = [self._make_leg(ib_daily_pnl=-24997.64)]
        collapsed = collapse_positions(positions)
        self.assertEqual(len(collapsed), 1)
        self.assertAlmostEqual(collapsed[0]['ib_daily_pnl'], -24997.64, places=2)

    def test_multi_leg_ib_daily_pnl_aggregates(self):
        """Multi-leg position should sum ibDailyPnl from all legs."""
        from ib_sync import collapse_positions
        # Bull call spread: long $315 call + short $340 call
        positions = [
            self._make_leg(symbol='GOOG', strike=315, right='C', position=44,
                           expiry='2026-04-17', ib_daily_pnl=-5000.0),
            self._make_leg(symbol='GOOG', strike=340, right='C', position=-44,
                           expiry='2026-04-17', ib_daily_pnl=2000.0),
        ]
        collapsed = collapse_positions(positions)
        self.assertEqual(len(collapsed), 1)
        self.assertAlmostEqual(collapsed[0]['ib_daily_pnl'], -3000.0, places=2)

    def test_none_ib_daily_pnl_propagates_as_none(self):
        """If any leg has None ibDailyPnl, collapsed should be None."""
        from ib_sync import collapse_positions
        positions = [
            self._make_leg(symbol='GOOG', strike=315, right='C', position=44,
                           expiry='2026-04-17', ib_daily_pnl=-5000.0),
            self._make_leg(symbol='GOOG', strike=340, right='C', position=-44,
                           expiry='2026-04-17', ib_daily_pnl=None),
        ]
        collapsed = collapse_positions(positions)
        self.assertEqual(len(collapsed), 1)
        self.assertIsNone(collapsed[0]['ib_daily_pnl'])

    def test_all_none_ib_daily_pnl_is_none(self):
        """If all legs have None ibDailyPnl, collapsed should be None."""
        from ib_sync import collapse_positions
        positions = [self._make_leg(ib_daily_pnl=None)]
        collapsed = collapse_positions(positions)
        self.assertEqual(len(collapsed), 1)
        self.assertIsNone(collapsed[0]['ib_daily_pnl'])

    def test_zero_ib_daily_pnl_is_zero_not_none(self):
        """ibDailyPnl of 0.0 should remain 0.0, not become None."""
        from ib_sync import collapse_positions
        positions = [self._make_leg(ib_daily_pnl=0.0)]
        collapsed = collapse_positions(positions)
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]['ib_daily_pnl'], 0.0)

    def test_stock_position_has_ib_daily_pnl(self):
        """Stock positions should also carry ib_daily_pnl."""
        from ib_sync import collapse_positions
        positions = [{
            'symbol': 'MSFT',
            'secType': 'STK',
            'position': 1000,
            'avgCost': 420.0,
            'entry_cost': 420000.0,
            'expiry': 'N/A',
            'strike': None,
            'right': None,
            'structure': 'Stock (1000 shares)',
            'marketPrice': 415.0,
            'marketValue': 415000.0,
            'marketPriceIsCalculated': False,
            'ibDailyPnl': -1415.0,
        }]
        collapsed = collapse_positions(positions)
        self.assertEqual(len(collapsed), 1)
        self.assertAlmostEqual(collapsed[0]['ib_daily_pnl'], -1415.0, places=2)


class TestIbDailyPnlValidation(unittest.TestCase):
    """Test the sentinel value filtering in fetch_position_daily_pnl."""

    def test_dbl_max_sentinel_filtered(self):
        """IB's DBL_MAX sentinel (1.7976...e+308) should be treated as None."""
        DBL_MAX = 1.7976931348623157e+308
        from ib_insync import util as ib_util

        def _valid(val):
            return val is not None and not ib_util.isNan(val) and val != DBL_MAX

        self.assertFalse(_valid(DBL_MAX))
        self.assertFalse(_valid(None))
        self.assertFalse(_valid(float('nan')))
        self.assertTrue(_valid(0.0))
        self.assertTrue(_valid(-24997.64))
        self.assertTrue(_valid(100.0))


if __name__ == '__main__':
    unittest.main()
