"""Tests for incremental_sync — skip full sync when no position changes detected.

Red/Green TDD: tests written FIRST (RED phase), then implementation follows.
Uses tmp files for portfolio.json — no real IB connection needed.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from utils.incremental_sync import incremental_sync, positions_changed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_portfolio_json(positions):
    """Create a temporary portfolio.json with given positions."""
    data = {
        "bankroll": 100000,
        "positions": positions,
        "last_sync": "2026-03-10T10:00:00",
    }
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, tmp)
    tmp.close()
    return Path(tmp.name)


def _make_ib_position(symbol, sec_type="OPT", position=5, expiry="20260417"):
    """Create a mock IB position object."""
    pos = MagicMock()
    pos.contract = MagicMock()
    pos.contract.symbol = symbol
    pos.contract.secType = sec_type
    pos.contract.lastTradeDateOrContractMonth = expiry
    pos.position = position
    pos.avgCost = 100.0
    return pos


# ---------------------------------------------------------------------------
# positions_changed detection
# ---------------------------------------------------------------------------


class TestPositionsChanged:
    """Test the positions_changed comparison logic."""

    def test_no_changes_returns_false(self):
        """Identical positions should return False (no changes)."""
        portfolio_positions = [
            {"ticker": "AAPL", "expiry": "2026-04-17", "contracts": 5},
            {"ticker": "GOOG", "expiry": "2026-03-20", "contracts": 10},
        ]
        ib_positions = [
            _make_ib_position("AAPL", position=5, expiry="20260417"),
            _make_ib_position("GOOG", position=10, expiry="20260320"),
        ]
        assert positions_changed(portfolio_positions, ib_positions) is False

    def test_position_added_returns_true(self):
        """A new position in IB should return True."""
        portfolio_positions = [
            {"ticker": "AAPL", "expiry": "2026-04-17", "contracts": 5},
        ]
        ib_positions = [
            _make_ib_position("AAPL", position=5, expiry="20260417"),
            _make_ib_position("TSLA", position=3, expiry="20260320"),
        ]
        assert positions_changed(portfolio_positions, ib_positions) is True

    def test_position_removed_returns_true(self):
        """A position gone from IB should return True."""
        portfolio_positions = [
            {"ticker": "AAPL", "expiry": "2026-04-17", "contracts": 5},
            {"ticker": "GOOG", "expiry": "2026-03-20", "contracts": 10},
        ]
        ib_positions = [
            _make_ib_position("AAPL", position=5, expiry="20260417"),
        ]
        assert positions_changed(portfolio_positions, ib_positions) is True

    def test_contract_count_changed_returns_true(self):
        """Changed contract count should return True."""
        portfolio_positions = [
            {"ticker": "AAPL", "expiry": "2026-04-17", "contracts": 5},
        ]
        ib_positions = [
            _make_ib_position("AAPL", position=10, expiry="20260417"),
        ]
        assert positions_changed(portfolio_positions, ib_positions) is True

    def test_empty_both_returns_false(self):
        """Both empty should return False."""
        assert positions_changed([], []) is False

    def test_stock_positions_use_na_expiry(self):
        """Stock positions (secType=STK) should use N/A as expiry key."""
        portfolio_positions = [
            {"ticker": "AAPL", "expiry": "N/A", "contracts": 100},
        ]
        ib_positions = [
            _make_ib_position("AAPL", sec_type="STK", position=100, expiry=""),
        ]
        assert positions_changed(portfolio_positions, ib_positions) is False


# ---------------------------------------------------------------------------
# incremental_sync full flow
# ---------------------------------------------------------------------------


class TestIncrementalSync:
    """Test the incremental_sync function."""

    def test_no_changes_skips_full_sync(self):
        """When positions match, should return existing portfolio without sync."""
        positions = [
            {"ticker": "AAPL", "expiry": "2026-04-17", "contracts": 5},
        ]
        portfolio_path = _make_portfolio_json(positions)

        ib_positions = [
            _make_ib_position("AAPL", position=5, expiry="20260417"),
        ]

        mock_client = MagicMock()
        mock_client.get_positions.return_value = ib_positions

        result = incremental_sync(mock_client, portfolio_path)

        assert result["changed"] is False
        assert result["portfolio"]["bankroll"] == 100000

        # Clean up
        portfolio_path.unlink()

    def test_changes_detected_triggers_full_sync(self):
        """When positions differ, should flag for full sync."""
        positions = [
            {"ticker": "AAPL", "expiry": "2026-04-17", "contracts": 5},
        ]
        portfolio_path = _make_portfolio_json(positions)

        ib_positions = [
            _make_ib_position("AAPL", position=5, expiry="20260417"),
            _make_ib_position("TSLA", position=3, expiry="20260320"),
        ]

        mock_client = MagicMock()
        mock_client.get_positions.return_value = ib_positions

        result = incremental_sync(mock_client, portfolio_path)

        assert result["changed"] is True

        portfolio_path.unlink()

    def test_missing_portfolio_file_triggers_full_sync(self):
        """If portfolio.json doesn't exist, should trigger full sync."""
        mock_client = MagicMock()
        mock_client.get_positions.return_value = [
            _make_ib_position("AAPL", position=5, expiry="20260417"),
        ]

        nonexistent = Path("/tmp/nonexistent_portfolio_test.json")
        if nonexistent.exists():
            nonexistent.unlink()

        result = incremental_sync(mock_client, nonexistent)

        assert result["changed"] is True

    def test_empty_ib_positions_with_existing_portfolio(self):
        """If IB has no positions but portfolio has some, flag changes."""
        positions = [
            {"ticker": "AAPL", "expiry": "2026-04-17", "contracts": 5},
        ]
        portfolio_path = _make_portfolio_json(positions)

        mock_client = MagicMock()
        mock_client.get_positions.return_value = []

        result = incremental_sync(mock_client, portfolio_path)

        assert result["changed"] is True

        portfolio_path.unlink()
