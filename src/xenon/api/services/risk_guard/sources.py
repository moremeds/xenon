"""Market sources and their freshness (spec §5.1).

P1 sources: IB ``SPX`` (cash index) and IB ``SPY``, both streamed on the api
``data`` role (two market-data lines). Futu ``SPY`` is a later, independent
source: no Python consumer of the futu-quotes stream exists yet.

A tick = a new ``Ticker.time`` with a live (``marketDataType == 1``) price.
ib_async stamps ``Ticker.time`` when it processes a message, so it is a
receive time, not an exchange time: the ``SKEW_S`` gate only catches a bad
API-host clock or a ticker read long after its update, and any tick on the
line (a size or bid tick too) keeps the source fresh even if ``last`` is
flat. ponytail: a per-price-change clock if a frozen ``last`` with live
size ticks is ever seen. Ages run on the monotonic receive clock. A source with no tick for
``STALE_S`` is stale. In RTH every SPX-family source stale = ``FEED_STALE``,
some stale = ``SOURCE_STALE``; fresh sources whose 5 m returns differ by more
than ``DIVERGENCE`` = ``FEED_DIVERGENCE``. Outside RTH staleness is shown, not
alerted (P1).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from xenon.api.services.risk_guard.windows import PriceWindow

logger = logging.getLogger(__name__)

STALE_S = 20.0
SKEW_S = 5.0
DIVERGENCE = 0.003  # 0.3 pp on the 5 m return
DIVERGENCE_WINDOW_S = 300


@dataclass(frozen=True)
class Quote:
    price: float | None
    prior_close: float | None
    source_time: datetime | None
    live: bool


@dataclass
class SourceState:
    name: str
    window: PriceWindow = field(default_factory=PriceWindow)
    prior_close: float | None = None
    last_rx: float | None = None  # monotonic receive time of the last accepted tick
    rejected: int = 0
    _src_time: datetime | None = None

    @property
    def last(self) -> float | None:
        return self.window.last

    def observe(self, now_mono: float, now_wall: datetime, q: Quote | None, session: str) -> None:
        if q is None or not q.live or q.price is None or q.source_time is None:
            return
        if q.source_time == self._src_time:
            return
        if abs((now_wall - q.source_time).total_seconds()) > SKEW_S:
            self.rejected += 1
            return
        self._src_time = q.source_time
        self.last_rx = now_mono
        self.prior_close = q.prior_close
        self.window.add(now_mono, q.price, session)

    def fresh(self, now_mono: float) -> bool:
        return self.last_rx is not None and now_mono - self.last_rx <= STALE_S

    def age(self, now_mono: float) -> float | None:
        return None if self.last_rx is None else now_mono - self.last_rx


def feed_health(sources: list[SourceState], now_mono: float, *, rth: bool) -> dict[str, str]:
    """BLIND causes from the market sources: cause -> detail."""
    causes: dict[str, str] = {}
    if not rth:
        return causes

    def age(s: SourceState) -> str:
        a = s.age(now_mono)
        return f"{s.name} {'never' if a is None else f'{a:.0f} s'}"

    stale = [s for s in sources if not s.fresh(now_mono)]
    if len(stale) == len(sources):
        causes["FEED_STALE"] = "no tick: " + ", ".join(age(s) for s in stale)
    elif stale:
        causes["SOURCE_STALE"] = "no tick: " + ", ".join(age(s) for s in stale)
    fresh_rets = [
        (s.name, r)
        for s in sources
        if s.fresh(now_mono) and (r := s.window.ret(now_mono, DIVERGENCE_WINDOW_S)) is not None
    ]
    if len(fresh_rets) >= 2:
        lo, hi = min(r for _, r in fresh_rets), max(r for _, r in fresh_rets)
        if hi - lo > DIVERGENCE:
            causes["FEED_DIVERGENCE"] = "5m " + ", ".join(f"{n} {r * 100:+.2f}%" for n, r in fresh_rets)
    return causes


def agree(values: list[float | None], *, blind: bool) -> float | None:
    """A price metric that two fresh sources agree on: the milder (higher) value.

    With one usable value it counts only while a BLIND cause is raised.
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    if len(vals) >= 2 and len(vals) == len(values):
        return max(vals)
    return max(vals) if blind else None


def _finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


class IBQuoteFeed:
    """SPX + SPY streaming tickers on the api ``data`` role.

    The subscription is re-made whenever the role's client object changes
    (a reconnect builds a new client) or IB drops the ticker. Reads go through
    ``ib_pool.call_data_bounded`` (role worker, deadline, upstream fast-fail),
    so a wedged gateway costs one bounded read, never a hang.
    """

    SYMBOLS = ("SPX", "SPY")
    TIMEOUT_S = 3.0
    RECONNECT_EVERY_S = 30.0

    def __init__(self, pool: Any) -> None:
        self.pool = pool
        self._client: Any = None
        self._contracts: dict[str, Any] = {}
        self.last_error: str | None = None
        self._reconnect_at: float | None = None

    def _contract(self, symbol: str) -> Any:
        from ib_async import Index, Stock

        return Index("SPX", "CBOE", "USD") if symbol == "SPX" else Stock(symbol, "SMART", "USD")

    def _read(self, client: Any) -> dict[str, Quote]:
        """Runs on the data role worker thread."""
        ib = client.ib
        if client is not self._client:
            self._client, self._contracts = client, {}
        out: dict[str, Quote] = {}
        for sym in self.SYMBOLS:
            contract = self._contracts.get(sym)
            ticker = ib.ticker(contract) if contract is not None else None
            if ticker is None:
                contract = self._contract(sym)
                ib.qualifyContracts(contract)
                ticker = ib.reqMktData(contract, "", False, False)
                self._contracts[sym] = contract
            out[sym] = Quote(
                price=_finite(ticker.last),
                prior_close=_finite(ticker.close),
                source_time=ticker.time,
                live=getattr(ticker, "marketDataType", 1) == 1,
            )
        return out

    async def read(self) -> dict[str, Quote]:
        """Quotes, or {} on any failure. A down data role is retried (the pool
        reconnects inside ``acquire``) at most every ``RECONNECT_EVERY_S``: the
        pool has no reconnect loop of its own, so without this a guard alone on
        the role would stay blind after a gateway restart."""
        if self.pool is None:
            return {}
        if not self.pool.is_connected("data"):
            now = time.monotonic()
            if self._reconnect_at is not None and now - self._reconnect_at < self.RECONNECT_EVERY_S:
                self.last_error = "data role not connected"
                return {}
            self._reconnect_at = now
        from xenon.api.ib_pool import call_data_bounded

        try:
            out = await call_data_bounded(self.pool, self._read, timeout=self.TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — a failed read is a stale source, never a crash
            if self.last_error != type(exc).__name__:
                logger.warning("risk guard IB read failed: %s", type(exc).__name__)
            self.last_error = type(exc).__name__
            return {}
        self.last_error = None
        return out
