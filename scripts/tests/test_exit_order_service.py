"""Tests for exit_order_service.py — expiry extraction from order data."""
import pytest
import re

from exit_order_service import extract_expiry


class TestExtractExpiry:
    """Test expiry extraction from order/trade data."""

    def test_expiry_from_legs_field(self):
        """When legs have an 'expiry' field, use it directly."""
        order = {
            "ticker": "GOOG",
            "legs": [
                {"type": "Long Call", "strike": 315, "expiry": "2026-04-17"},
                {"type": "Short Call", "strike": 340, "expiry": "2026-04-17"},
            ],
        }
        assert extract_expiry(order) == "20260417"

    def test_expiry_from_legs_yyyymmdd_format(self):
        """Legs with expiry already in YYYYMMDD format."""
        order = {
            "ticker": "AAPL",
            "legs": [
                {"type": "Long Call", "strike": 200, "expiry": "20260515"},
                {"type": "Short Call", "strike": 220, "expiry": "20260515"},
            ],
        }
        assert extract_expiry(order) == "20260515"

    def test_expiry_from_contract_description(self):
        """Parse expiry from contract string like 'GOOG Apr 17, 2026 $315/$340 Call Spread'."""
        order = {
            "ticker": "GOOG",
            "contract": "GOOG Apr 17, 2026 $315/$340 Call Spread",
            "legs": [
                {"type": "Long Call", "strike": 315},
                {"type": "Short Call", "strike": 340},
            ],
        }
        assert extract_expiry(order) == "20260417"

    def test_expiry_from_contract_month_name_formats(self):
        """Various month names in contract description."""
        order = {
            "ticker": "SPY",
            "contract": "SPY Jan 15, 2027 $500/$520 Call Spread",
            "legs": [
                {"type": "Long Call", "strike": 500},
                {"type": "Short Call", "strike": 520},
            ],
        }
        assert extract_expiry(order) == "20270115"

    def test_expiry_from_contract_mar_format(self):
        """Test Mar abbreviation in contract description."""
        order = {
            "ticker": "EWY",
            "contract": "EWY Mar 13, 2026 $148/$140 Put Spread",
            "legs": [
                {"type": "Long Put", "strike": 148},
                {"type": "Short Put", "strike": 140},
            ],
        }
        assert extract_expiry(order) == "20260313"

    def test_fallback_when_no_expiry_available(self):
        """When no expiry can be parsed, return None and let caller handle fallback."""
        order = {
            "ticker": "XYZ",
            "legs": [
                {"type": "Long Call", "strike": 100},
                {"type": "Short Call", "strike": 120},
            ],
        }
        assert extract_expiry(order) is None

    def test_empty_legs(self):
        """Empty legs list returns None."""
        order = {"ticker": "XYZ", "legs": []}
        assert extract_expiry(order) is None

    def test_no_legs_key(self):
        """Missing legs key returns None (unless contract has info)."""
        order = {"ticker": "XYZ"}
        assert extract_expiry(order) is None

    def test_mixed_legs_first_expiry_wins(self):
        """If only first leg has expiry, still extract it."""
        order = {
            "ticker": "GOOG",
            "legs": [
                {"type": "Long Call", "strike": 315, "expiry": "2026-04-17"},
                {"type": "Short Call", "strike": 340},
            ],
        }
        assert extract_expiry(order) == "20260417"
