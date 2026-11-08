"""Price windows on a 1 s grid (spec §5.1 window rules).

Times are monotonic receive seconds. A tick lands on its whole second; the last
value is carried forward until the next tick. A window return compares the last
price with the grid value at ``now - W``; it is ``None`` (UNAVAILABLE, never 0%)
while the series is shorter than the window (warm-up, 2 s tolerance) or stale.

A tick gap > ``reconnect_gap_s`` restarts the series. A session change restarts
it and also clears the session high / low.
"""

from __future__ import annotations

import math
from collections import deque


class PriceWindow:
    def __init__(self, max_window_s: int = 3600, reconnect_gap_s: float = 20.0, tolerance_s: int = 2) -> None:
        self.max_window_s = max_window_s
        self.reconnect_gap_s = reconnect_gap_s
        self.tolerance_s = tolerance_s
        self.session: str | None = None
        self.high: float | None = None
        self.low: float | None = None
        self._samples: deque[tuple[int, float]] = deque()
        self._last_t: float | None = None

    @property
    def last(self) -> float | None:
        return self._samples[-1][1] if self._samples else None

    @property
    def last_t(self) -> float | None:
        return self._last_t

    def add(self, t: float, price: float, session: str) -> None:
        if not math.isfinite(price) or price <= 0:
            return
        if session != self.session:
            self.session, self.high, self.low = session, None, None
            self._samples.clear()
        elif self._last_t is not None:
            if t < self._last_t:
                return
            if t - self._last_t > self.reconnect_gap_s:
                self._samples.clear()
        sec = int(t)
        if self._samples and self._samples[-1][0] == sec:
            self._samples[-1] = (sec, price)
        else:
            self._samples.append((sec, price))
        self._last_t = t
        self.high = price if self.high is None else max(self.high, price)
        self.low = price if self.low is None else min(self.low, price)
        # Keep one sample at or before the oldest window start as the carry-forward anchor.
        cutoff = sec - self.max_window_s - self.tolerance_s
        while len(self._samples) >= 2 and self._samples[1][0] <= cutoff:
            self._samples.popleft()

    def ret(self, now: float, window_s: int) -> float | None:
        if not self._samples or self._last_t is None or now - self._last_t > self.reconnect_gap_s:
            return None
        target = int(now) - window_s
        first_sec, first_price = self._samples[0]
        if first_sec > target + self.tolerance_s:
            return None
        base = first_price
        for sec, price in self._samples:
            if sec > target:
                break
            base = price
        return self._samples[-1][1] / base - 1.0


def day_return(price: float | None, prior_close: float | None) -> float | None:
    """Return since the official prior RTH close; ``None`` without both inputs."""
    if price is None or not prior_close:
        return None
    return price / prior_close - 1.0
