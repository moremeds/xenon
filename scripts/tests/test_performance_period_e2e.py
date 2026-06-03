"""``compute()`` with the period parameter — end-to-end against PG."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from xenon.api.services.perf_cache import cached_compute, clear_cache
from xenon.api.services.performance import compute
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

pytestmark = pytest.mark.asyncio

_SCOPE = AccountScope(broker="IB", account_env="live", broker_account="IB_PE2E1")


def _seed_nav(scope, navs: list[tuple[date, float]]) -> None:
    for d, n in navs:
        upsert_nav_sync(scope=scope, day=d, nav=Decimal(str(n)), source="close")


async def test_period_ytd_default(async_engine):
    _seed_nav(
        _SCOPE,
        [
            (date(2025, 12, 1), 100.0),
            (date(2026, 1, 2), 101.0),
            (date(2026, 6, 1), 110.0),
        ],
    )
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3))
    # Default = YTD → window [2026-01-01, 2026-06-03]. 12/1 excluded.
    assert result["status"] == "ok"
    assert result["period_start"] == "2026-01-01"
    assert len(result["series"]) == 2


async def test_period_1m_narrows_to_30_days(async_engine):
    _seed_nav(
        _SCOPE,
        [
            (date(2026, 1, 2), 100.0),
            (date(2026, 4, 1), 105.0),
            (date(2026, 5, 30), 108.0),
            (date(2026, 6, 1), 110.0),
        ],
    )
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3), period="1M")
    # 1M back from 2026-06-03 = 2026-05-04 — only 5/30 and 6/1 qualify.
    assert result["status"] == "ok"
    assert len(result["series"]) == 2
    assert result["period_start"] == "2026-05-04"


async def test_period_all_uses_inception(async_engine):
    _seed_nav(
        _SCOPE,
        [
            (date(2024, 8, 1), 100.0),
            (date(2026, 6, 1), 200.0),
        ],
    )
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3), period="All")
    assert result["status"] == "ok"
    assert len(result["series"]) == 2
    assert result["period_start"] == "2024-08-01"


async def test_invalid_period_raises(async_engine):
    from xenon.api.services.performance_periods import InvalidPeriodError

    _seed_nav(_SCOPE, [(date(2026, 1, 2), 100.0)])
    with pytest.raises(InvalidPeriodError):
        await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3), period="6M")


async def test_cache_key_includes_period(async_engine):
    """Two period values must NOT share a cache entry."""
    clear_cache()
    _seed_nav(
        _SCOPE,
        [
            (date(2026, 1, 2), 100.0),
            (date(2026, 5, 30), 105.0),
            (date(2026, 6, 1), 110.0),
        ],
    )
    ytd = await cached_compute(async_engine, _SCOPE, period="YTD")
    m1 = await cached_compute(async_engine, _SCOPE, period="1M")
    # Two distinct results — proves the cache didn't return YTD for 1M.
    assert ytd["period_start"] != m1["period_start"]


async def test_summary_includes_new_return_fields(async_engine):
    _seed_nav(
        _SCOPE,
        [
            (date(2026, 1, 2), 100.0),
            (date(2026, 6, 1), 110.0),
        ],
    )
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3))
    summary = result["summary"]
    assert "simple_total_return" in summary
    assert "twr_total_return" in summary
    assert "irr_total_return" in summary
    assert "net_external_flows" in summary
    # IB has no flow source → simple equals total_return; twr/irr stay None.
    assert summary["simple_total_return"] == pytest.approx(summary["total_return"])
    assert summary["net_external_flows"] == 0.0
    assert summary["twr_total_return"] is None
    assert summary["irr_total_return"] is None
