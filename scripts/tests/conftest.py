"""Shared pytest configuration and fixtures for scripts tests."""

import sys
from pathlib import Path

import pytest

# Add both the repo root and scripts/ so tests can import via either
# `scripts.*` package paths or the legacy bare module paths.
PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "trade_blotter"))

# ── Shared fixtures ──────────────────────────────────────────────────────
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytz

EASTERN = pytz.timezone("America/New_York")


@pytest.fixture
def mock_ib_client():
    """Pre-configured IB client mock with common methods."""
    client = MagicMock()
    client.reqMktData = MagicMock()
    client.placeOrder = MagicMock()
    client.reqPositions = MagicMock()
    client.isConnected = MagicMock(return_value=True)
    return client


@pytest.fixture
def mock_uw_client():
    """UW API client mock matching actual uw_client.py method signatures."""
    client = MagicMock()
    client.get_flow_alerts = MagicMock(return_value=[])
    client.get_flow_alerts_by_ticker = MagicMock(return_value=[])
    client.get_flow_per_strike = MagicMock(return_value={})
    client.get_flow_per_expiry = MagicMock(return_value={})
    return client


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Temp directory patched into server DATA_DIR."""
    monkeypatch.setattr("api.server.DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def frozen_market_time():
    """Context manager to freeze market hours to a fixed ET time.

    Usage:
        with frozen_market_time(hour=10, minute=30):
            assert is_market_open() is True
        with frozen_market_time(hour=17, minute=0):
            assert is_market_open() is False
    """
    from datetime import timedelta

    @contextmanager
    def _freeze(hour, minute, weekday=0):
        base = datetime(2026, 4, 13, hour, minute)  # Monday
        offset = (weekday - base.weekday()) % 7
        target = base + timedelta(days=offset)
        fake_now = EASTERN.localize(target.replace(hour=hour, minute=minute))
        with patch("utils.market_hours.get_eastern_now", return_value=fake_now):
            yield fake_now

    return _freeze
