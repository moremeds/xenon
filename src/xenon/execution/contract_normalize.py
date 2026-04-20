"""Contract normalization: expiry, ticker, multiplier.

Pure functions. No I/O. Called at the API boundary so downstream
code (OrderTab.tsx, ib_place_order.py, nakedShortGuard.ts) can
stop reimplementing these locally.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §9
"""

import datetime as _dt
import re

from xenon.execution.universe import get_multiplier, is_known

_ASCII_8DIGITS = re.compile(r"^[0-9]{8}$")


class NormalizationError(ValueError):
    """Raised when input cannot be normalized to a canonical form."""


def normalize_expiry(value: str | None) -> str:
    """Normalize an expiry string to IB canonical `YYYYMMDD`.

    Accepts: `20260117`, `2026-01-17`, `2026/01/17`.
    Rejects: anything else, including `None`, empty, month/day out of
    range, or impossible dates like 2023-02-29.
    """
    if not value or not isinstance(value, str):
        raise NormalizationError(f"expiry must be a non-empty string, got {value!r}")

    cleaned = value.strip().replace("-", "").replace("/", "")
    if not _ASCII_8DIGITS.match(cleaned):
        raise NormalizationError(f"expiry must be 8 ASCII digits after cleaning, got {cleaned!r}")

    try:
        parsed = _dt.date(int(cleaned[0:4]), int(cleaned[4:6]), int(cleaned[6:8]))
    except ValueError as e:
        raise NormalizationError(f"invalid calendar date {cleaned!r}: {e}") from e

    return parsed.strftime("%Y%m%d")


def normalize_ticker(value: str | None) -> str:
    """Normalize a ticker to its canonical uppercase form.

    Rejects tickers not in the V1 universe.
    """
    if not value or not isinstance(value, str):
        raise NormalizationError(f"ticker must be a non-empty string, got {value!r}")

    candidate = value.strip().upper()
    if not is_known(candidate):
        raise NormalizationError(f"ticker {candidate!r} not in V1 universe")

    return candidate


def resolve_multiplier(ticker: str) -> int:
    """Return the option contract multiplier for a V1 ticker.

    Raises NormalizationError if the ticker is unknown.
    """
    try:
        return get_multiplier(ticker)
    except KeyError as e:
        raise NormalizationError(f"ticker {ticker!r} not in V1 universe") from e
