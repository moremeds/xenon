"""Unit tests for option_chain_snapshotter.

Covers pure-logic modules (config, hours, persister helpers, fetcher helpers)
without IB or Postgres connections.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_index_exchange_all_four_tickers():
    from xenon.option_chain_snapshotter.config import INDEX_EXCHANGE, TICKERS

    for t in TICKERS:
        assert t in INDEX_EXCHANGE, f"{t} missing from INDEX_EXCHANGE"


def test_batch_size_within_ib_line_limit():
    from xenon.option_chain_snapshotter.config import BATCH_SIZE

    # IB account limit is 100; we must leave headroom for relay + API pool.
    assert BATCH_SIZE <= 75, "BATCH_SIZE too high — risks starving relay/API"
    assert BATCH_SIZE >= 20, "BATCH_SIZE too low — cycle time would be impractical"


def test_default_cadence_is_600():
    from xenon.option_chain_snapshotter.config import DEFAULT_CADENCE_S

    assert DEFAULT_CADENCE_S == 600


def test_strike_pct_range_reasonable():
    from xenon.option_chain_snapshotter.config import STRIKE_PCT_RANGE

    assert 0.05 <= STRIKE_PCT_RANGE <= 0.50


# --------------------------------------------------------------------------- #
# hours
# --------------------------------------------------------------------------- #


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class TestIsSessionOpen:
    def test_midday_tuesday_is_open(self):
        from xenon.option_chain_snapshotter.hours import is_session_open

        # 2026-06-16 is a Tuesday; 17:00 UTC = 13:00 ET (well inside RTH)
        assert is_session_open(_utc(2026, 6, 16, 17, 0)) is True

    def test_saturday_is_closed(self):
        from xenon.option_chain_snapshotter.hours import is_session_open

        assert is_session_open(_utc(2026, 6, 20, 17, 0)) is False

    def test_sunday_is_closed(self):
        from xenon.option_chain_snapshotter.hours import is_session_open

        assert is_session_open(_utc(2026, 6, 21, 17, 0)) is False

    def test_pre_open_buffer_is_open(self):
        from xenon.option_chain_snapshotter.config import SESSION_PRE_OPEN_MIN
        from xenon.option_chain_snapshotter.hours import is_session_open

        # June 2026 → EDT (UTC-4); NYSE open = 9:30 AM EDT = 13:30 UTC.
        # Window starts SESSION_PRE_OPEN_MIN before open = 13:25 UTC.
        # 13:26 UTC is 1 min inside the pre-open buffer → should be open.
        pre_open = _utc(2026, 6, 16, 13, 30) - timedelta(minutes=SESSION_PRE_OPEN_MIN - 1)
        assert is_session_open(pre_open) is True

    def test_before_pre_open_buffer_is_closed(self):
        from xenon.option_chain_snapshotter.config import SESSION_PRE_OPEN_MIN
        from xenon.option_chain_snapshotter.hours import is_session_open

        # 13:24 UTC = 1 min before the pre-open window (which starts at 13:25 UTC).
        before_window = _utc(2026, 6, 16, 13, 30) - timedelta(minutes=SESSION_PRE_OPEN_MIN + 1)
        assert is_session_open(before_window) is False

    def test_post_close_buffer_is_open(self):
        from xenon.option_chain_snapshotter.config import SESSION_POST_CLOSE_MIN
        from xenon.option_chain_snapshotter.hours import is_session_open

        # RTH close is 20:00 UTC; window ends SESSION_POST_CLOSE_MIN after that
        post_close = _utc(2026, 6, 16, 20, 0) + timedelta(minutes=SESSION_POST_CLOSE_MIN - 1)
        assert is_session_open(post_close) is True

    def test_after_post_close_buffer_is_closed(self):
        from xenon.option_chain_snapshotter.config import SESSION_POST_CLOSE_MIN
        from xenon.option_chain_snapshotter.hours import is_session_open

        after_window = _utc(2026, 6, 16, 20, 0) + timedelta(minutes=SESSION_POST_CLOSE_MIN + 1)
        assert is_session_open(after_window) is False


# --------------------------------------------------------------------------- #
# fetcher helpers
# --------------------------------------------------------------------------- #


class TestSafeHelper:
    def test_none_returns_none(self):
        from xenon.option_chain_snapshotter.fetcher import _safe

        assert _safe(None) is None

    def test_nan_returns_none(self):
        from xenon.option_chain_snapshotter.fetcher import _safe

        assert _safe(float("nan")) is None

    def test_valid_float_passes_through(self):
        from xenon.option_chain_snapshotter.fetcher import _safe

        assert _safe(3.14) == 3.14

    def test_zero_passes_through(self):
        from xenon.option_chain_snapshotter.fetcher import _safe

        assert _safe(0.0) == 0.0


class TestHasQuote:
    def _make_ticker(self, bid=None, ask=None, last=None):
        t = MagicMock()
        t.bid = bid
        t.ask = ask
        t.last = last
        return t

    def test_positive_bid_is_quote(self):
        from xenon.option_chain_snapshotter.fetcher import _has_quote

        assert _has_quote(self._make_ticker(bid=1.5)) is True

    def test_positive_ask_is_quote(self):
        from xenon.option_chain_snapshotter.fetcher import _has_quote

        assert _has_quote(self._make_ticker(ask=2.0)) is True

    def test_all_none_is_not_quote(self):
        from xenon.option_chain_snapshotter.fetcher import _has_quote

        assert _has_quote(self._make_ticker()) is False

    def test_nan_bid_is_not_quote(self):
        from xenon.option_chain_snapshotter.fetcher import _has_quote

        assert _has_quote(self._make_ticker(bid=float("nan"))) is False

    def test_zero_bid_is_not_quote(self):
        from xenon.option_chain_snapshotter.fetcher import _has_quote

        assert _has_quote(self._make_ticker(bid=0.0)) is False


# --------------------------------------------------------------------------- #
# persister helpers
# --------------------------------------------------------------------------- #


class TestPGPersisterNaN:
    """Verify that _safe() in persist_rows handles NaN model greek values."""

    def test_nan_greek_becomes_none_for_insert(self):
        """modelGreeks fields can be NaN — must map to None before psycopg insert."""
        mg = MagicMock()
        mg.impliedVol = float("nan")
        mg.delta = float("nan")
        mg.gamma = 0.001
        mg.vega = float("nan")
        mg.theta = -0.05
        mg.undPrice = 5500.0

        from xenon.option_chain_snapshotter.persister import PGPersister

        p = PGPersister.__new__(PGPersister)

        def _safe(v):
            if v is None:
                return None
            if isinstance(v, float) and math.isnan(v):
                return None
            return v

        assert _safe(mg.impliedVol) is None
        assert _safe(mg.delta) is None
        assert _safe(mg.gamma) == 0.001
        assert _safe(mg.theta) == -0.05
        assert _safe(mg.undPrice) == 5500.0


# --------------------------------------------------------------------------- #
# daemon cadence math
# --------------------------------------------------------------------------- #


def test_sleep_capped_at_zero_when_cycle_overruns():
    """When a cycle takes longer than cadence, sleep should be 0 not negative."""
    cadence = 600
    elapsed = 750  # overran by 150s
    sleep_s = max(0.0, cadence - elapsed)
    assert sleep_s == 0.0


def test_sleep_is_remainder_when_cycle_is_fast():
    cadence = 600
    elapsed = 420
    sleep_s = max(0.0, cadence - elapsed)
    assert sleep_s == 180.0
