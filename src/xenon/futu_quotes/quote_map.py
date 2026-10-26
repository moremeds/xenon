"""Futu QUOTE row → relay PriceData mapping.

Rules (docs/architecture/futu-quotes.md):

- ``timestamp`` is ISO-8601 UTC ``Z`` built from ``data_date`` + ``data_time``
  read as America/New_York wall time with DST handling. It is never the
  receive, flush or reconnect time.
- Every numeric field is a finite JSON number or ``None``; an invalid raw
  field (``'N/A'``, ``'NaN'``, NaN/inf, non-numeric) becomes ``None``.
- The whole quote is dropped only when ``last`` or the timestamp is invalid.
- A per-symbol high-water mark drops quotes older than the last one
  forwarded; the mark resets when the symbol is unsubscribed.
- US overnight session (``overnight_row_to_pricedata``): ``last`` /
  ``high`` / ``low`` / ``volume`` come from the ``overnight_*`` fields,
  ``close`` is the RTH ``last_price``, ``open`` is null and ``timestamp`` is
  the receipt time (Futu carries no overnight time). The row is dropped when
  ``overnight_price`` is missing, non-finite or ≤ 0.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def et_wall_to_utc(data_date: Any, data_time: Any) -> Optional[datetime]:
    """Parse a Futu ET wall date+time into an aware UTC datetime.

    Returns ``None`` when either input is unparseable. Ambiguous times
    (fall-back hour) resolve to the first occurrence (fold=0); RTH quotes
    never land in that window.
    """
    if not isinstance(data_date, str) or not isinstance(data_time, str):
        return None
    data_date = data_date.strip()
    data_time = data_time.strip()
    try:
        d = datetime.strptime(data_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    t = None
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            t = datetime.strptime(data_time, fmt).time()
            break
        except ValueError:
            continue
    if t is None:
        return None
    aware = datetime.combine(d, t, tzinfo=ET)
    return aware.astimezone(timezone.utc)


def et_wall_to_iso_z(data_date: Any, data_time: Any) -> Optional[str]:
    """ET wall date+time → ISO-8601 UTC string with a ``Z`` suffix."""
    ts = et_wall_to_utc(data_date, data_time)
    if ts is None:
        return None
    return ts.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def finite_or_none(value: Any) -> Optional[float]:
    """Coerce a raw Futu cell to a finite float or ``None``.

    Tolerates ``'N/A'``, ``''``, ``'NaN'``, NaN/inf, non-numeric and
    list/dict cells. Booleans are rejected.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _int_or_none(value: Any) -> Optional[Any]:
    f = finite_or_none(value)
    if f is None:
        return None
    return int(f) if f.is_integer() else f


# PriceData fields Futu's QUOTE push never carries.
_NULL_FIELDS: Mapping[str, None] = {
    "bid": None,
    "ask": None,
    "bidSize": None,
    "askSize": None,
    "week52High": None,
    "week52Low": None,
    "avgVolume": None,
    "delta": None,
    "gamma": None,
    "theta": None,
    "vega": None,
    "impliedVol": None,
    "undPrice": None,
}


def row_to_pricedata(ticker: str, row: Mapping[str, Any]) -> Optional[tuple[dict, datetime]]:
    """Map one Futu QUOTE row to ``(PriceData+source, utc_ts)``.

    ``ticker`` is the plain relay-side symbol (``US.AAPL`` → ``AAPL`` is the
    caller's job). Returns ``None`` when ``last_price`` or the timestamp is
    invalid — the only drop conditions.
    """
    ts = et_wall_to_utc(row.get("data_date"), row.get("data_time"))
    last = finite_or_none(row.get("last_price"))
    if ts is None or last is None:
        return None
    pd: dict = {
        "symbol": ticker,
        "last": last,
        "lastIsCalculated": False,
        **_NULL_FIELDS,
    }
    pd["volume"] = _int_or_none(row.get("volume"))
    pd["high"] = finite_or_none(row.get("high_price"))
    pd["low"] = finite_or_none(row.get("low_price"))
    pd["open"] = finite_or_none(row.get("open_price"))
    pd["close"] = finite_or_none(row.get("prev_close_price"))
    pd["timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    pd["source"] = "futu"
    return pd, ts


def overnight_row_to_pricedata(
    ticker: str, row: Mapping[str, Any], received_at: datetime
) -> Optional[tuple[dict, datetime]]:
    """Map one Futu QUOTE row in the US overnight session.

    ``received_at`` is the aware UTC receipt time; it is the timestamp
    because the QUOTE push has no overnight time field (``data_date`` /
    ``data_time`` still name the last RTH quote). Returns ``None`` when
    ``overnight_price`` is missing, non-finite or ≤ 0 (no overnight trade,
    e.g. a holiday night).
    """
    last = finite_or_none(row.get("overnight_price"))
    if last is None or last <= 0:
        return None
    ts = received_at.astimezone(timezone.utc)
    pd: dict = {
        "symbol": ticker,
        "last": last,
        "lastIsCalculated": False,
        **_NULL_FIELDS,
    }
    pd["volume"] = _int_or_none(row.get("overnight_volume"))
    pd["high"] = finite_or_none(row.get("overnight_high_price"))
    pd["low"] = finite_or_none(row.get("overnight_low_price"))
    pd["open"] = None
    pd["close"] = finite_or_none(row.get("last_price"))  # latest RTH close
    pd["timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    pd["source"] = "futu"
    pd["session"] = "overnight"
    return pd, ts


class OvernightBaseline:
    """Per-symbol last ``(overnight_volume, overnight_price)`` seen.

    Overnight rows are stamped with receipt time, so a cached push replayed
    after a subscribe would look fresh. ``changed`` is True only when the
    pair differs from the last pair seen for the symbol; the first row after
    a reset is the baseline and returns False.

    ponytail: ceiling — a quiet symbol publishes nothing until its next
    overnight trade changes volume or price, even though it is Futu-``ok``.
    """

    def __init__(self) -> None:
        self._last: dict[str, tuple[Optional[float], Optional[float]]] = {}

    def changed(self, symbol: str, row: Mapping[str, Any]) -> bool:
        key = (
            finite_or_none(row.get("overnight_volume")),
            finite_or_none(row.get("overnight_price")),
        )
        prev = self._last.get(symbol)
        self._last[symbol] = key
        return prev is not None and key != prev

    def reset(self, symbol: str) -> None:
        self._last.pop(symbol, None)

    def reset_all(self) -> None:
        self._last.clear()


class HighWaterMark:
    """Per-symbol source-timestamp high-water mark.

    ``accept`` returns True only when ``ts`` is not older than the last
    accepted timestamp for that symbol. ``reset`` is called on unsubscribe,
    so a replayed cached quote after a re-subscribe is judged fresh only by
    its true source time.
    """

    def __init__(self) -> None:
        self._marks: dict[str, datetime] = {}

    def accept(self, symbol: str, ts: datetime) -> bool:
        prev = self._marks.get(symbol)
        if prev is not None and ts < prev:
            return False
        self._marks[symbol] = ts
        return True

    def seen(self, symbol: str, ts: datetime) -> bool:
        prev = self._marks.get(symbol)
        return prev is not None and ts < prev

    def reset(self, symbol: str) -> None:
        self._marks.pop(symbol, None)

    def reset_all(self) -> None:
        self._marks.clear()
