"""FRED FX backfill + cached USD conversion against xenon.fx_daily.

Direction convention: currency_per_usd stores the foreign-currency-per-USD
rate (FRED's native form). amount_usd = amount_original / currency_per_usd.

Used by the Futu history reconstruction pipeline to convert HKD/CNY
cash-flow rows into USD before they hit the equity-curve math.
"""

from __future__ import annotations

import csv
import io
import os
import time
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

import httpx
from sqlalchemy import create_engine, text

FRED_SERIES = {"HKD": "DEXHKUS", "CNY": "DEXCHUS"}
_FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_RETRIES = 3
_BACKOFF_S = (1.0, 2.0, 4.0)


class FxRateMissing(Exception):
    """No fx_daily row for (currency, date)."""


@dataclass
class FxBackfillFailed(Exception):
    reason_code: str = "fx_backfill_failed"

    def __str__(self) -> str:  # pragma: no cover
        return self.reason_code


def get_sync_engine():
    # Use psycopg3 driver (matches the rest of xenon — psycopg2 is not installed).
    url = (os.environ.get("DATABASE_URL_TEST") or os.environ["DATABASE_URL"]).replace(
        "postgresql+asyncpg://", "postgresql+psycopg://"
    )
    return create_engine(url, future=True)


def _http_get(url: str, params: dict) -> httpx.Response:
    """Indirection seam so tests can patch the HTTP boundary."""
    with httpx.Client(timeout=30.0) as client:
        return client.get(url, params=params)


def _fetch_fred_csv(series_id: str, start: date, end: date) -> str:
    params = {
        "id": series_id,
        "cosd": start.isoformat(),
        "coed": end.isoformat(),
    }
    for attempt in range(_RETRIES):
        try:
            resp = _http_get(_FRED_URL, params)
            if resp.status_code == 200:
                return resp.text
        except httpx.HTTPError:
            pass
        if attempt < _RETRIES - 1:
            time.sleep(_BACKOFF_S[attempt])
    raise FxBackfillFailed()


def _parse_fred_csv(body: str, series_id: str) -> Iterable[tuple[date, Decimal]]:
    reader = csv.DictReader(io.StringIO(body))
    for row in reader:
        rate_str = row.get(series_id, "").strip()
        if rate_str in {"", ".", "ND"}:
            continue
        d = date.fromisoformat(row["observation_date"])
        yield d, Decimal(rate_str)


def backfill_fx_series(currency: str, start: date, end: date) -> int:
    """Pull series from FRED, upsert into xenon.fx_daily. Returns row count."""
    series_id = FRED_SERIES[currency]
    body = _fetch_fred_csv(series_id, start, end)
    rows = list(_parse_fred_csv(body, series_id))
    eng = get_sync_engine()
    with eng.begin() as conn:
        for d, rate in rows:
            conn.execute(
                text(
                    "INSERT INTO xenon.fx_daily (currency, date, currency_per_usd, source) "
                    "VALUES (:c, :d, :r, 'FRED') "
                    "ON CONFLICT (currency, date) DO UPDATE SET "
                    "currency_per_usd = EXCLUDED.currency_per_usd"
                ),
                {"c": currency, "d": d, "r": rate},
            )
    return len(rows)


def convert_to_usd(amount: Decimal, currency: str, d: date) -> Decimal:
    """Divide original by currency_per_usd. Raises FxRateMissing if absent."""
    if currency == "USD":
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    eng = get_sync_engine()
    with eng.connect() as conn:
        rate = conn.execute(
            text("SELECT currency_per_usd FROM xenon.fx_daily WHERE currency=:c AND date=:d"),
            {"c": currency, "d": d},
        ).scalar_one_or_none()
    if rate is None:
        raise FxRateMissing(f"no fx_daily row for {currency} on {d}")
    return (amount / Decimal(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
