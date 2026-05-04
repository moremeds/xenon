"""Per-tick mark/spot cache. Spec §8 mark/spot coalescing."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    ts: datetime


def is_quote_fresh(quote: Quote, *, now: datetime | None = None, max_age_s: int = 60) -> bool:
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - quote.ts) <= timedelta(seconds=max_age_s)


class MarkCache:
    def __init__(self, fetcher: Callable[[int], Quote | None]):
        self._fetcher = fetcher
        self._cache: dict[int, Quote | None] = {}

    def get(self, *, con_id: int) -> Quote | None:
        if con_id not in self._cache:
            self._cache[con_id] = self._fetcher(con_id)
        return self._cache[con_id]


class SpotCache:
    def __init__(self, fetcher: Callable[[str], Quote | None]):
        self._fetcher = fetcher
        self._cache: dict[str, Quote | None] = {}

    def get(self, *, symbol: str) -> Quote | None:
        if symbol not in self._cache:
            self._cache[symbol] = self._fetcher(symbol)
        return self._cache[symbol]
