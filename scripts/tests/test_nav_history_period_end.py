"""``load_nav_curve`` ``period_end`` + ``load_inception_date`` + prefer-close."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from xenon.db.queries.nav_history import load_inception_date, load_nav_curve
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

pytestmark = pytest.mark.asyncio

_SCOPE = AccountScope(broker="FUTU", account_env="live", broker_account="FUTU_PE1")


def _seed(scope: AccountScope, days_navs: list[tuple[date, float]]) -> None:
    for d, nav in days_navs:
        upsert_nav_sync(scope=scope, day=d, nav=Decimal(str(nav)), source="close")


async def test_load_nav_curve_respects_period_end(async_engine):
    _seed(
        _SCOPE,
        [
            (date(2025, 1, 15), 100.0),
            (date(2025, 2, 15), 110.0),
            (date(2025, 3, 15), 105.0),
            (date(2025, 4, 15), 120.0),
        ],
    )
    df = await load_nav_curve(
        async_engine,
        _SCOPE,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
    )
    assert list(df["date"]) == [date(2025, 1, 15), date(2025, 2, 15), date(2025, 3, 15)]


async def test_load_nav_curve_period_end_none_keeps_old_behavior(async_engine):
    _seed(
        _SCOPE,
        [
            (date(2025, 1, 15), 100.0),
            (date(2025, 5, 15), 130.0),
        ],
    )
    df = await load_nav_curve(async_engine, _SCOPE, period_start=date(2025, 1, 1), period_end=None)
    assert list(df["date"]) == [date(2025, 1, 15), date(2025, 5, 15)]


async def test_load_inception_returns_min_date(async_engine):
    _seed(
        _SCOPE,
        [
            (date(2024, 8, 1), 100.0),
            (date(2025, 1, 15), 110.0),
        ],
    )
    inception = await load_inception_date(async_engine, _SCOPE)
    assert inception == date(2024, 8, 1)


async def test_load_inception_returns_none_when_no_rows(async_engine):
    empty_scope = AccountScope(broker="FUTU", account_env="live", broker_account="FUTU_EMPTY_PE")
    inception = await load_inception_date(async_engine, empty_scope)
    assert inception is None


async def test_load_nav_curve_returns_intraday_when_only_intraday_exists(async_engine):
    """Same-source seed: DISTINCT ON returns one row per date as before."""
    upsert_nav_sync(
        scope=_SCOPE,
        day=date(2025, 9, 1),
        nav=Decimal("100.00"),
        source="intraday",
    )
    upsert_nav_sync(
        scope=_SCOPE,
        day=date(2025, 9, 2),
        nav=Decimal("101.00"),
        source="intraday",
    )
    df = await load_nav_curve(async_engine, _SCOPE, period_start=date(2025, 9, 1))
    rows = df[df["date"].isin([date(2025, 9, 1), date(2025, 9, 2)])]
    assert list(rows["source"]) == ["intraday", "intraday"]
    assert list(rows["nav"]) == [100.0, 101.0]


async def test_load_nav_curve_returns_close_when_only_close_exists(async_engine):
    """Same shape with source='close' — DISTINCT ON still returns one row per date."""
    upsert_nav_sync(scope=_SCOPE, day=date(2025, 10, 1), nav=Decimal("100.00"), source="close")
    upsert_nav_sync(scope=_SCOPE, day=date(2025, 10, 2), nav=Decimal("101.00"), source="close")
    df = await load_nav_curve(async_engine, _SCOPE, period_start=date(2025, 10, 1))
    rows = df[df["date"].isin([date(2025, 10, 1), date(2025, 10, 2)])]
    assert list(rows["source"]) == ["close", "close"]


async def test_load_nav_curve_prefers_close_when_both_exist_same_date(async_engine):
    """Pass-1 / Pass-2 E1(a): when intraday + close rows coexist for the same
    date, the prefer-close DISTINCT ON returns the close row body."""
    scope = AccountScope(broker="FUTU", account_env="live", broker_account="FUTU_PREFER_PE")
    upsert_nav_sync(
        scope=scope,
        day=date(2025, 12, 1),
        nav=Decimal("100.00"),
        source="intraday",
    )
    upsert_nav_sync(
        scope=scope,
        day=date(2025, 12, 1),
        nav=Decimal("100.50"),
        source="close",
    )
    df = await load_nav_curve(async_engine, scope, period_start=date(2025, 12, 1))
    row = df[df["date"] == date(2025, 12, 1)].iloc[0]
    assert row["source"] == "close"
    assert row["nav"] == pytest.approx(100.50)
