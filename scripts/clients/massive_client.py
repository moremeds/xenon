"""Massive.com REST client for historical OHLCV aggregates.

Scope (v1): /v2/aggs/ticker/{T}/range/{m}/{timespan}/{from}/{to}
Timeframes: 1d, 1h. Returns ET-normalized OHLCV + VWAP + tx_count.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_BASE_URL_DEFAULT = "https://api.massive.com"
_HEALTH_PATH = "/v1/marketstatus/now"
_TIMEFRAME_MAP: dict[str, tuple[int, str]] = {
    "1d": (1, "day"),
    "1h": (1, "hour"),
}
_BACKOFF_SECONDS = (1.0, 2.0, 4.0)


class MassiveError(Exception):
    """Base class for MassiveClient failures."""


class MassiveAuthError(MassiveError):
    """Auth failed — no retry."""


class MassiveNoDataError(MassiveError):
    """Ticker unknown or empty results — skip, no retry."""


class MassiveRateLimitError(MassiveError):
    """Retries exhausted for 429 / 5xx / network failure."""


class MassiveClient:
    """Thin REST client. Thread-safe for read operations via shared Session."""

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 10.0,
        base_url: str | None = None,
    ) -> None:
        load_dotenv()
        self._api_key = api_key or os.environ.get("MASSIVE_API_KEY")
        if not self._api_key:
            raise MassiveAuthError("MASSIVE_API_KEY not set in environment")
        self._timeout = timeout
        self._base_url = (base_url or os.environ.get("MASSIVE_BASE_URL") or _BASE_URL_DEFAULT).rstrip("/")
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {self._api_key}"

    def get_aggregates(
        self,
        ticker: str,
        timeframe: str,
        from_date: str,
        to_date: str,
        adjusted: bool = True,
    ) -> pd.DataFrame:
        if timeframe not in _TIMEFRAME_MAP:
            raise ValueError(f"Unsupported timeframe: {timeframe!r}")
        multiplier, timespan = _TIMEFRAME_MAP[timeframe]
        url = f"{self._base_url}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        params = {
            "adjusted": "true" if adjusted else "false",
            "sort": "asc",
            "limit": 50000,
        }

        attempts = len(_BACKOFF_SECONDS) + 1
        for attempt in range(attempts):
            try:
                resp = self._session.get(url, params=params, timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt < attempts - 1:
                    time.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise MassiveRateLimitError(f"Network failure after {attempts} attempts: {exc}") from exc

            status = resp.status_code
            if status == 200:
                payload = resp.json()
                results = payload.get("results") or []
                if not results:
                    raise MassiveNoDataError(f"No data for {ticker} {timeframe} {from_date}..{to_date}")
                return self._rows_to_dataframe(results)

            if status in (401, 403):
                raise MassiveAuthError(f"Auth failed ({status}) for {url}")
            if status == 404:
                raise MassiveNoDataError(f"Not found for {ticker}")
            if status == 429 or 500 <= status < 600:
                if attempt < attempts - 1:
                    time.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise MassiveRateLimitError(f"HTTP {status} after {attempts} attempts")

            raise MassiveError(f"Unexpected status {status}: {resp.text[:200]}")

        raise MassiveRateLimitError("Unreachable retry loop")

    @staticmethod
    def _rows_to_dataframe(results: list[dict[str, Any]]) -> pd.DataFrame:
        rows = []
        for r in results:
            ts_utc = pd.Timestamp(r["t"], unit="ms", tz="UTC")
            ts_et = ts_utc.tz_convert(_ET)
            rows.append(
                {
                    "date": ts_et,
                    "open": float(r["o"]),
                    "high": float(r["h"]),
                    "low": float(r["l"]),
                    "close": float(r["c"]),
                    "volume": int(r["v"]),
                    "vwap": float(r["vw"]) if r.get("vw") is not None else 0.0,
                    "tx_count": int(r["n"]) if r.get("n") is not None else 0,
                }
            )
        df = pd.DataFrame(rows)
        df.sort_values("date", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def health_check(self) -> bool:
        try:
            resp = self._session.get(f"{self._base_url}{_HEALTH_PATH}", timeout=self._timeout)
            return resp.status_code == 200
        except requests.RequestException:
            return False
