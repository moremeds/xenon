"""Typed exception hierarchy for the Futu OpenD adapter.

The Futu SDK raises generic exceptions with free-form error strings.
`classify_futu_exception` converts those into typed errors so callers
can branch on error kind (rate-limit backoff, reconnect, hard fail)
without string-matching everywhere.
"""

from __future__ import annotations


class FutuError(Exception):
    """Base exception for all Futu adapter errors."""


class FutuConnectionError(FutuError):
    """Connection to Futu OpenD lost or unavailable."""


class FutuRateLimitError(FutuError):
    """Futu API rate limit exceeded.

    Futu enforces 10 calls / 30s for `position_list_query` and
    `accinfo_query`. `cooldown_seconds` is how long the caller should
    wait before retrying.
    """

    def __init__(self, message: str = "Rate limit exceeded", cooldown_seconds: int = 30):
        super().__init__(message)
        self.cooldown_seconds = cooldown_seconds


class FutuDataError(FutuError):
    """Invalid, missing, or unexpected data from Futu API."""


class FutuAuthError(FutuError):
    """Authentication / authorization error (unlock trade password, etc.)."""


def classify_futu_exception(e: Exception) -> FutuError:
    """Classify a generic exception into a typed FutuError.

    Inspects the error string. The original exception is chained via
    `__cause__` so the full traceback is preserved.
    """
    if isinstance(e, FutuError):
        return e

    msg = str(e).lower()

    if "disconnect" in msg or "connection" in msg or "socket" in msg or "timed out" in msg:
        exc: FutuError = FutuConnectionError(f"Connection error: {e}")
    elif "frequent" in msg or "rate" in msg or "too many" in msg or "limit" in msg:
        exc = FutuRateLimitError(f"Rate limited: {e}")
    elif "auth" in msg or "permission" in msg or "unlock" in msg or "password" in msg:
        exc = FutuAuthError(f"Authentication error: {e}")
    else:
        exc = FutuDataError(f"Futu error: {e}")

    exc.__cause__ = e
    return exc
