"""Tests for perf_cache (spec Decisions §6)."""
import datetime as dt
import zoneinfo
from unittest.mock import AsyncMock, patch

import pytest

from xenon.api.services.perf_cache import _ttl_for_now, cached_compute, clear_cache
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.asyncio

_ET = zoneinfo.ZoneInfo("America/New_York")


# ---------- TTL ----------


def test_ttl_open_during_rth():
    # Mon 10:00 ET
    assert _ttl_for_now(dt.datetime(2026, 6, 1, 10, 0, tzinfo=_ET)) == 60


def test_ttl_closed_after_market():
    # Mon 20:00 ET
    assert _ttl_for_now(dt.datetime(2026, 6, 1, 20, 0, tzinfo=_ET)) == 1800


def test_ttl_closed_before_market():
    # Mon 08:00 ET
    assert _ttl_for_now(dt.datetime(2026, 6, 1, 8, 0, tzinfo=_ET)) == 1800


def test_ttl_closed_on_weekend():
    # Sat 12:00 ET
    assert _ttl_for_now(dt.datetime(2026, 6, 6, 12, 0, tzinfo=_ET)) == 1800
    # Sun 14:00 ET
    assert _ttl_for_now(dt.datetime(2026, 6, 7, 14, 0, tzinfo=_ET)) == 1800


def test_ttl_boundary_9_30_open():
    """9:30 ET → open (inclusive lower bound)."""
    assert _ttl_for_now(dt.datetime(2026, 6, 1, 9, 30, tzinfo=_ET)) == 60


def test_ttl_boundary_16_00_closed():
    """16:00 ET → closed (exclusive upper bound)."""
    assert _ttl_for_now(dt.datetime(2026, 6, 1, 16, 0, tzinfo=_ET)) == 1800


# ---------- cache hits / misses ----------


@patch("xenon.api.services.performance.compute", new_callable=AsyncMock)
async def test_cache_hit_returns_same_object(mock_compute):
    mock_compute.return_value = {"v": 1}
    clear_cache()
    scope = AccountScope("IB", "paper", "DU1")
    r1 = await cached_compute(None, scope, ib_pool=None)
    r2 = await cached_compute(None, scope, ib_pool=None)
    assert r1 is r2
    assert mock_compute.call_count == 1


@patch("xenon.api.services.performance.compute", new_callable=AsyncMock)
async def test_different_scopes_have_independent_cache(mock_compute):
    mock_compute.side_effect = [{"v": 1}, {"v": 2}]
    clear_cache()
    await cached_compute(None, AccountScope("IB", "paper", "DU1"), ib_pool=None)
    await cached_compute(None, AccountScope("FUTU", "live", "42"), ib_pool=None)
    assert mock_compute.call_count == 2
