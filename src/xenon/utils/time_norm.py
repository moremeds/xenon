"""UTC timestamp normalization.

Every timestamp that crosses a process boundary in Xenon MUST be
UTC, ISO 8601 with `Z` suffix, millisecond precision. Naive datetimes
raise ValueError — there is no "local time" policy, period.

Conversion to America/New_York happens at the UI render layer only,
via the existing `marketCalendar` helpers on the web side.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Union

from zoneinfo import ZoneInfo

_FUTU_DEFAULT_TZ = ZoneInfo("Asia/Hong_Kong")
_US_EASTERN_TZ = ZoneInfo("America/New_York")


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    """Serialize an aware datetime as UTC ISO 8601 with `Z` suffix, ms precision.

    Raises:
        ValueError: if `dt` is naive (no tzinfo).
    """
    if dt.tzinfo is None:
        raise ValueError(
            "iso_z() refuses naive datetimes — attach tzinfo explicitly. "
            "A silently-assumed local time zone is the bug this function exists to prevent."
        )
    dt_utc = dt.astimezone(timezone.utc)
    # Format with ms precision; strftime with %f gives microseconds, truncate.
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt_utc.microsecond // 1000:03d}Z"


def parse_iso_z(s: str) -> datetime:
    """Parse an ISO 8601 `Z`-suffixed string into an aware UTC datetime.

    Accepts both `Z` and `+00:00` suffixes. Non-UTC offsets are converted
    to UTC. Rejects naive strings (no tzinfo).

    Raises:
        ValueError: on unparseable input or missing tzinfo.
    """
    if not isinstance(s, str) or not s:
        raise ValueError(f"parse_iso_z() expected non-empty str, got {type(s).__name__}: {s!r}")

    # fromisoformat() in Python 3.11+ handles "Z", but normalize anyway for 3.9/3.10 compat.
    normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"parse_iso_z() could not parse {s!r}: {exc}") from exc

    if dt.tzinfo is None:
        raise ValueError(f"parse_iso_z() refuses naive datetime string: {s!r}")
    return dt.astimezone(timezone.utc)


def from_futu_naive(
    dt: Union[datetime, str],
    tz: ZoneInfo = _FUTU_DEFAULT_TZ,
) -> datetime:
    """Interpret a Futu SDK timestamp as being in `tz` and convert to UTC.

    Futu returns timestamps as naive datetimes (or strings) in the local
    time zone of the market. For US-market data, set `tz` to America/New_York;
    for account-level timestamps Futu typically uses Asia/Hong_Kong.

    Both `datetime` and `str` (ISO-ish) inputs are accepted. If the input
    already carries tzinfo, it is returned converted to UTC unchanged.
    """
    if isinstance(dt, str):
        # Accept 'YYYY-MM-DD HH:MM:SS' (space separator) or ISO.
        candidate = dt.replace(" ", "T", 1) if " " in dt and "T" not in dt else dt
        parsed = datetime.fromisoformat(candidate)
    elif isinstance(dt, datetime):
        parsed = dt
    else:
        raise ValueError(f"from_futu_naive() expected str or datetime, got {type(dt).__name__}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


def from_futu_us_eastern(dt: Union[datetime, str]) -> datetime:
    """Convenience: interpret a Futu US-market timestamp as US/Eastern → UTC."""
    return from_futu_naive(dt, tz=_US_EASTERN_TZ)
