"""Per-tick mark/spot cache. Spec §8 mark/spot coalescing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from xenon.execution.brackets.executor.marks import (
    MarkCache,
    Quote,
    SpotCache,
    is_quote_fresh,
)


def test_quote_fresh_within_window():
    now = datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)
    quote = Quote(symbol="AAPL", price=190.10, ts=now - timedelta(seconds=5))
    assert is_quote_fresh(quote, now=now, max_age_s=60) is True


def test_quote_stale_after_window():
    now = datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)
    quote = Quote(symbol="AAPL", price=190.10, ts=now - timedelta(seconds=120))
    assert is_quote_fresh(quote, now=now, max_age_s=60) is False


def test_mark_cache_coalesces_within_tick():
    calls = {"n": 0}

    def fetch(con_id):
        calls["n"] += 1
        return Quote(symbol="AAPL", price=190.10, ts=datetime.now(timezone.utc))

    cache = MarkCache(fetcher=fetch)
    cache.get(con_id=12345)
    cache.get(con_id=12345)
    assert calls["n"] == 1


def test_spot_cache_coalesces_per_symbol():
    calls = {"n": 0}

    def fetch(symbol):
        calls["n"] += 1
        return Quote(symbol=symbol, price=580.0, ts=datetime.now(timezone.utc))

    cache = SpotCache(fetcher=fetch)
    cache.get(symbol="SPY")
    cache.get(symbol="SPY")
    cache.get(symbol="QQQ")
    assert calls["n"] == 2
