# Massive.com Historical Data Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace IB's rate-limited historical OHLCV fetches with Massive.com REST API in the `ta_lib` cache pipeline. Clean EOD cutoff — Massive owns ≤ yesterday, IB keeps live quotes/chains/execution. Yahoo removed.

**Architecture:** New `MassiveClient` in `scripts/clients/` (REST, raw `requests`, follows `UWClient` pattern). `ta_lib/bars.py` rewrites to call Massive instead of IB. `TAService` drops `ib_client` and IB-specific pacing. `ta_premarket_prep` + `ta_cli` + `trend_scan.py` switch to Massive via lazy-constructed client. Status file (`data/ta_premarket_status.json`) carries abort state to the scanner.

**Tech Stack:** Python 3.13, `requests`, `pandas`, `python-dotenv`, `pytest` + `responses` for HTTP mocks, DuckDB (unchanged), `zoneinfo` for ET/DST.

**Spec:** `docs/superpowers/specs/2026-04-15-massive-historical-data-migration-design.md`

**Review history:** Rev 2 — revised after bilateral tribunal review (Codex + Claude, Gemini rate-limited). Fixed 15 findings: merged Tasks 3+4 into single atomic change (bars.py and TAService must land together); added `BulkRefreshResult` return contract so `ta_premarket_prep` can compute real failure ratio; fixed `_is_stale` string-vs-date parsing + missing `time` import; ET-aware `date.today()`; `refresh_tickers` computed before service construction; `ta_cli` keeps client when not `--cache-only`; `ta_reseed_massive` uses explicit `lookback_days` and passes universe file paths; added `output["warnings"]` contract to `trend_scan`; stale-status-file age check; E2E-2 uses `read_ohlc` to verify persistence; test fixture imports and mocks corrected; `main() -> int` return type.

---

## File Structure

**New:**

- `scripts/clients/massive_client.py` — `MassiveClient` (REST, auth, retry, exceptions, ET normalization)
- `scripts/tests/test_massive_client.py` — unit tests (HTTP mocked via `responses`)
- `scripts/tests/test_e2e_massive_pipeline.py` — E2E tests (`@pytest.mark.e2e`, opt-in)
- `scripts/ta_reseed_massive.py` — operator re-seed script (loops both timeframes)
- `scripts/api_status/ta_premarket_status.py` — typed read/write helpers for `data/ta_premarket_status.json` (small, isolated)
- `scripts/tests/test_ta_premarket_status.py` — unit tests for status file helpers

**Modified:**

- `scripts/ta_lib/bars.py` — full rewrite (Massive-backed)
- `scripts/ta_lib/service.py` — drop IB, add Massive, update `_is_stale`, remove `_ib_sleep`/pacing, refresh both timeframes
- `scripts/ta_premarket_prep.py` — lazy client, both timeframes, status file, exit code 3
- `scripts/ta_cli.py` — lazy client
- `scripts/trend_scan.py` — new `TAService` instantiation, read status file
- `scripts/tests/test_ta_lib/test_bars.py` — rewrite for Massive
- `scripts/tests/test_ta_lib/test_service.py` — constructor change, drop `ib_client=`
- `scripts/tests/test_ta_lib/test_snapshot_contract.py` — same
- `scripts/tests/test_ta_lib/test_premarket_prep.py` — add offline + both-timeframe regressions
- `pyproject.toml` — add `e2e` marker
- `CLAUDE.md` (root) — Data Source Priority, credentials table
- `scripts/CLAUDE.md` — `ta_lib` section + command table
- `CHANGELOG.md` — migration entry
- `.env` (root) — `MASSIVE_API_KEY` (operator step, not committed)

**Deleted:**

- `scripts/ta_seed_yahoo.py`
- `scripts/tests/test_ta_lib/test_seed_yahoo.py`

---

## Preconditions

Before Task 1:

- `MASSIVE_API_KEY` obtained and set in root `.env`
- Smoke-tested via curl:
  ```bash
  curl -sS -H "Authorization: Bearer $MASSIVE_API_KEY" \
    "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/2026-04-01/2026-04-14?adjusted=true&sort=asc&limit=50" | head -c 500
  ```
  Expected: JSON with `results` array containing OHLCV rows.

---

## Task 1: `MassiveClient` — client, auth, exceptions, retry

**Files:**

- Create: `scripts/clients/massive_client.py`
- Create: `scripts/tests/test_massive_client.py`
- Modify: `pyproject.toml` (add `responses` test dep if not present)

- [ ] **Step 1: Verify `responses` is available**

Run: `python3.13 -c "import responses; print(responses.__version__)"`
If missing, add to test deps in `pyproject.toml` under `[project.optional-dependencies].test` and install.

- [ ] **Step 2: Write failing tests in `scripts/tests/test_massive_client.py`**

```python
"""Unit tests for scripts.clients.massive_client."""

from __future__ import annotations

import os
from unittest.mock import patch

import pandas as pd
import pytest
import responses

from scripts.clients.massive_client import (
    MassiveAuthError,
    MassiveClient,
    MassiveNoDataError,
    MassiveRateLimitError,
)


_BASE = "https://api.massive.com"
_RESULTS_OK = {
    "ticker": "AAPL",
    "adjusted": True,
    "results": [
        {"t": 1744070400000, "o": 170.0, "h": 172.0, "l": 169.5, "c": 171.5,
         "v": 50_000_000, "vw": 170.8, "n": 450_000},
        {"t": 1744156800000, "o": 171.5, "h": 173.0, "l": 170.9, "c": 172.7,
         "v": 48_000_000, "vw": 171.9, "n": 430_000},
    ],
}


@pytest.fixture
def client():
    with patch.dict(os.environ, {"MASSIVE_API_KEY": "test-key"}, clear=False):
        yield MassiveClient(base_url=_BASE)


def test_missing_key_raises_at_init():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(MassiveAuthError):
            MassiveClient()


@responses.activate
def test_get_aggregates_1d_happy_path(client):
    responses.get(
        f"{_BASE}/v2/aggs/ticker/AAPL/range/1/day/2026-04-08/2026-04-09",
        json=_RESULTS_OK,
        status=200,
    )
    df = client.get_aggregates("AAPL", "1d", "2026-04-08", "2026-04-09")
    assert list(df.columns) == ["date", "open", "high", "low", "close",
                                 "volume", "vwap", "tx_count"]
    assert len(df) == 2
    assert df["close"].iloc[0] == 171.5
    # ET timezone on timestamps
    assert str(df["date"].iloc[0].tz) == "America/New_York"
    # ascending
    assert df["date"].is_monotonic_increasing


@responses.activate
def test_get_aggregates_1h_timeframe_map(client):
    responses.get(
        f"{_BASE}/v2/aggs/ticker/AAPL/range/1/hour/2026-04-08/2026-04-09",
        json=_RESULTS_OK,
        status=200,
    )
    df = client.get_aggregates("AAPL", "1h", "2026-04-08", "2026-04-09")
    assert len(df) == 2


@responses.activate
def test_adjusted_false_propagates(client):
    rsp = responses.get(
        f"{_BASE}/v2/aggs/ticker/AAPL/range/1/day/2026-04-08/2026-04-09",
        json=_RESULTS_OK,
        status=200,
    )
    client.get_aggregates("AAPL", "1d", "2026-04-08", "2026-04-09", adjusted=False)
    assert "adjusted=false" in rsp.calls[0].request.url


def test_invalid_timeframe_raises_value_error(client):
    with pytest.raises(ValueError):
        client.get_aggregates("AAPL", "5m", "2026-04-08", "2026-04-09")


@responses.activate
def test_401_raises_auth_error_no_retry(client):
    responses.get(
        f"{_BASE}/v2/aggs/ticker/AAPL/range/1/day/2026-04-08/2026-04-09",
        status=401,
    )
    with pytest.raises(MassiveAuthError):
        client.get_aggregates("AAPL", "1d", "2026-04-08", "2026-04-09")
    assert len(responses.calls) == 1  # no retry


@responses.activate
def test_404_raises_no_data_error(client):
    responses.get(
        f"{_BASE}/v2/aggs/ticker/ZZZZ/range/1/day/2026-04-08/2026-04-09",
        status=404,
    )
    with pytest.raises(MassiveNoDataError):
        client.get_aggregates("ZZZZ", "1d", "2026-04-08", "2026-04-09")


@responses.activate
def test_empty_results_raises_no_data(client):
    responses.get(
        f"{_BASE}/v2/aggs/ticker/AAPL/range/1/day/2026-04-08/2026-04-09",
        json={"ticker": "AAPL", "results": []},
        status=200,
    )
    with pytest.raises(MassiveNoDataError):
        client.get_aggregates("AAPL", "1d", "2026-04-08", "2026-04-09")


@responses.activate
def test_429_retries_then_raises(client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        "scripts.clients.massive_client.time.sleep",
        lambda s: sleeps.append(s),
    )
    for _ in range(4):
        responses.get(
            f"{_BASE}/v2/aggs/ticker/AAPL/range/1/day/2026-04-08/2026-04-09",
            status=429,
        )
    with pytest.raises(MassiveRateLimitError):
        client.get_aggregates("AAPL", "1d", "2026-04-08", "2026-04-09")
    assert len(responses.calls) == 4  # initial + 3 retries
    assert len(sleeps) == 3


@responses.activate
def test_health_check_ok(client):
    responses.get(f"{_BASE}/v1/marketstatus/now", json={"market": "extended-hours"},
                  status=200)
    assert client.health_check() is True


@responses.activate
def test_health_check_failure(client):
    responses.get(f"{_BASE}/v1/marketstatus/now", status=500)
    assert client.health_check() is False
```

- [ ] **Step 3: Run tests and verify they fail**

Run: `python3.13 -m pytest scripts/tests/test_massive_client.py -xvs`
Expected: FAIL with `ModuleNotFoundError: scripts.clients.massive_client`.

- [ ] **Step 4: Implement `scripts/clients/massive_client.py`**

```python
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
        self._base_url = (
            base_url or os.environ.get("MASSIVE_BASE_URL") or _BASE_URL_DEFAULT
        ).rstrip("/")
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
        url = (
            f"{self._base_url}/v2/aggs/ticker/{ticker}"
            f"/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        )
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
                raise MassiveRateLimitError(
                    f"Network failure after {attempts} attempts: {exc}"
                ) from exc

            status = resp.status_code
            if status == 200:
                payload = resp.json()
                results = payload.get("results") or []
                if not results:
                    raise MassiveNoDataError(
                        f"No data for {ticker} {timeframe} {from_date}..{to_date}"
                    )
                return self._rows_to_dataframe(results)

            if status in (401, 403):
                raise MassiveAuthError(f"Auth failed ({status}) for {url}")
            if status == 404:
                raise MassiveNoDataError(f"Not found for {ticker}")
            if status == 429 or 500 <= status < 600:
                if attempt < attempts - 1:
                    time.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise MassiveRateLimitError(
                    f"HTTP {status} after {attempts} attempts"
                )

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
            resp = self._session.get(
                f"{self._base_url}{_HEALTH_PATH}", timeout=self._timeout
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False
```

- [ ] **Step 5: Run tests — all pass**

Run: `python3.13 -m pytest scripts/tests/test_massive_client.py -xvs`
Expected: 11 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/clients/massive_client.py scripts/tests/test_massive_client.py
git commit -m "feat(clients): MassiveClient REST client with ET tz + retries"
```

---

## Task 2: Status-file helpers (`ta_premarket_status.json`)

**Files:**

- Create: `scripts/api_status/__init__.py` (empty)
- Create: `scripts/api_status/ta_premarket_status.py`
- Create: `scripts/tests/test_ta_premarket_status.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for scripts.api_status.ta_premarket_status."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.api_status.ta_premarket_status import (
    TaPremarketStatus,
    load_status,
    save_status,
)


def test_roundtrip(tmp_path: Path):
    path = tmp_path / "status.json"
    status = TaPremarketStatus(
        last_run_ts=datetime(2026, 4, 15, 6, 5, tzinfo=timezone.utc),
        status="ok",
        refreshed_tickers=247,
        failed_tickers=3,
        failure_ratio=0.012,
        failure_reason=None,
    )
    save_status(path, status)
    loaded = load_status(path)
    assert loaded == status


def test_load_missing_returns_none(tmp_path: Path):
    assert load_status(tmp_path / "nope.json") is None


def test_load_malformed_returns_none(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert load_status(path) is None


def test_aborted_status(tmp_path: Path):
    path = tmp_path / "status.json"
    status = TaPremarketStatus(
        last_run_ts=datetime(2026, 4, 15, 6, 5, tzinfo=timezone.utc),
        status="aborted",
        refreshed_tickers=50,
        failed_tickers=200,
        failure_ratio=0.80,
        failure_reason="vendor_down",
    )
    save_status(path, status)
    raw = json.loads(path.read_text())
    assert raw["status"] == "aborted"
    assert raw["failure_reason"] == "vendor_down"
```

- [ ] **Step 2: Run tests, verify fail**

Run: `python3.13 -m pytest scripts/tests/test_ta_premarket_status.py -xvs`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `scripts/api_status/ta_premarket_status.py`**

```python
"""Typed read/write for data/ta_premarket_status.json.

Records outcome of the 6 AM ET premarket refresh so trend_scan can
surface staleness at 8:30 AM ET.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

_Status = Literal["ok", "aborted", "partial"]


@dataclass
class TaPremarketStatus:
    last_run_ts: datetime
    status: _Status
    refreshed_tickers: int
    failed_tickers: int
    failure_ratio: float
    failure_reason: Optional[str]


def save_status(path: Path, status: TaPremarketStatus) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(status)
    payload["last_run_ts"] = status.last_run_ts.isoformat()
    path.write_text(json.dumps(payload, indent=2))


def load_status(path: Path) -> Optional[TaPremarketStatus]:
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return TaPremarketStatus(
            last_run_ts=datetime.fromisoformat(raw["last_run_ts"]),
            status=raw["status"],
            refreshed_tickers=int(raw["refreshed_tickers"]),
            failed_tickers=int(raw["failed_tickers"]),
            failure_ratio=float(raw["failure_ratio"]),
            failure_reason=raw.get("failure_reason"),
        )
    except (ValueError, KeyError) as exc:
        logger.warning("Failed to parse ta_premarket_status.json: %s", exc)
        return None
```

- [ ] **Step 4: Run tests, all pass**

Run: `python3.13 -m pytest scripts/tests/test_ta_premarket_status.py -xvs`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/api_status/ scripts/tests/test_ta_premarket_status.py
git commit -m "feat(ta_lib): ta_premarket_status.json helpers for run state"
```

---

## Task 3: Atomic `bars.py` + `TAService` rewrite (merged — MUST land together)

> **Why merged:** `bars.py` and `service.py` call each other. Splitting would leave an uncompilable intermediate state and break task-by-task execution. Single commit, multi-file.

**Files:**

- Modify: `scripts/ta_lib/bars.py` (full rewrite)
- Modify: `scripts/ta_lib/service.py` (multiple changes)
- Modify: `scripts/tests/test_ta_lib/test_bars.py` (rewrite)
- Modify: `scripts/tests/test_ta_lib/test_service.py` (constructor + new contract tests)
- Modify: `scripts/tests/test_ta_lib/test_snapshot_contract.py` (constructor)

### Key contracts introduced

**`BulkRefreshResult` dataclass** — returned by `TAService.bulk_refresh()` so callers can compute real failure ratios (replaces returning `None`):

```python
@dataclass
class BulkRefreshResult:
    attempted: int
    succeeded: int
    failed: int
    failed_tickers: list[str]
    aborted: bool  # True if hit internal stop condition
```

**`fetch_bars()` new signature** — takes `massive_client`, `timeframe`, `lookback_days`, `overlap_days=5`, `end_date=""`; uses ET-aware default for `to_dt`.

**`TAService.__init__`** — drops `ib_client`, adds `massive_client`. `read_only()` classmethod updated.

**`bulk_refresh(tickers, timeframe=None)`** — if `timeframe is None`, loops `("1d", "1h")`. No IB pacing. Returns `BulkRefreshResult`.

**`_is_stale()` for `1h`** — "latest bar ≥ prior-trading-day's 15:30 ET close"; parses `get_last_n_trading_days` string output correctly.

- [ ] **Step 1: Rewrite test file to target new signature**

Replace `scripts/tests/test_ta_lib/test_bars.py` with:

```python
"""Tests for scripts.ta_lib.bars (Massive-backed)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from scripts.ta_lib.bars import fetch_bars


def _massive_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": pd.Timestamp("2026-04-08", tz="America/New_York"),
             "open": 170.0, "high": 172.0, "low": 169.5, "close": 171.5,
             "volume": 50_000_000, "vwap": 170.8, "tx_count": 450_000},
            {"date": pd.Timestamp("2026-04-09", tz="America/New_York"),
             "open": 171.5, "high": 173.0, "low": 170.9, "close": 172.7,
             "volume": 48_000_000, "vwap": 171.9, "tx_count": 430_000},
        ]
    )


def test_fetch_bars_returns_store_compatible_columns():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    df = fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=30)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]


def test_fetch_bars_passes_overlap_days():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=30, overlap_days=5)
    args, kwargs = mc.get_aggregates.call_args
    # from_date should be lookback + overlap days behind to_date
    assert kwargs["ticker"] == "AAPL"
    assert kwargs["timeframe"] == "1d"


def test_fetch_bars_1h_timeframe():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    fetch_bars(mc, "AAPL", timeframe="1h", lookback_days=7)
    assert mc.get_aggregates.call_args.kwargs["timeframe"] == "1h"


def test_fetch_bars_filters_malformed_rows():
    bad = _massive_df().copy()
    bad.loc[len(bad)] = {
        "date": pd.Timestamp("2026-04-10", tz="America/New_York"),
        "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0,
        "volume": -1, "vwap": 0.0, "tx_count": 0,
    }
    mc = MagicMock()
    mc.get_aggregates.return_value = bad
    df = fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=30)
    assert len(df) == 2  # invalid row dropped


def test_fetch_bars_invalid_timeframe():
    mc = MagicMock()
    with pytest.raises(ValueError):
        fetch_bars(mc, "AAPL", timeframe="5m", lookback_days=30)
```

- [ ] **Step 2: Run tests, verify fail**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_bars.py -xvs`
Expected: FAIL (old `fetch_bars` signature).

- [ ] **Step 3: Rewrite `scripts/ta_lib/bars.py`**

```python
"""Massive-backed historical OHLCV fetch with Massive → DataFrame → store contract conversion.

Contract: returns a DataFrame with columns [date, open, high, low, close, volume]
(dropping Massive's VWAP and tx_count — Phase 2 persistence).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_SUPPORTED_TIMEFRAMES = {"1d", "1h"}
_STORE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def _today_et() -> date:
    """Return today's date in America/New_York — avoids host-local TZ skew."""
    return datetime.now(_ET).date()


def fetch_bars(
    massive_client,
    ticker: str,
    timeframe: str,
    lookback_days: int,
    overlap_days: int = 5,
    end_date: str = "",
) -> pd.DataFrame:
    """Fetch historical bars from Massive and return a store-compatible DataFrame.

    Args:
        massive_client: An initialized MassiveClient.
        ticker: Stock symbol (e.g. "AAPL").
        timeframe: "1d" or "1h".
        lookback_days: Calendar days of history to request.
        overlap_days: Extra days included before the lookback window to
            re-fetch recent bars (split / corrected-bar safety). Default 5,
            matching the IB-era `days_behind + 5` convention in service.py.
        end_date: End date as "YYYY-MM-DD". Empty = today in ET.

    Returns:
        DataFrame [date, open, high, low, close, volume], ascending, malformed rows filtered.
    """
    if timeframe not in _SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {timeframe!r}")

    to_dt = date.fromisoformat(end_date) if end_date else _today_et()
    from_dt = to_dt - timedelta(days=lookback_days + overlap_days)

    df = massive_client.get_aggregates(
        ticker=ticker,
        timeframe=timeframe,
        from_date=from_dt.isoformat(),
        to_date=to_dt.isoformat(),
        adjusted=True,
    )

    df = df[_STORE_COLUMNS].copy()  # drop VWAP + tx_count for v1 schema

    # Sanity filter
    before = len(df)
    df = df[
        (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0)
        & (df["close"] > 0) & (df["volume"] >= 0)
    ].copy()
    dropped = before - len(df)
    if dropped:
        logger.warning(
            "fetch_bars: dropped %d malformed row(s) for %s %s", dropped, ticker, timeframe
        )

    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df
```

- [ ] **Step 4: Refactor `scripts/ta_lib/service.py`**

Apply these surgical changes:

**(a) Add imports near the top of service.py:**

```python
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta  # add `time` for _is_stale
```

**(b) Add `BulkRefreshResult` dataclass at module level (before `class TAService`):**

```python
@dataclass
class BulkRefreshResult:
    attempted: int
    succeeded: int
    failed: int
    failed_tickers: list[str] = field(default_factory=list)
    aborted: bool = False

    @property
    def failure_ratio(self) -> float:
        return self.failed / max(self.attempted, 1)
```

**(c) Rename attribute and drop `_ib_sleep`:**

- `__init__`: `ib_client=None` → `massive_client=None`; `self._ib_client = ib_client` → `self._massive_client = massive_client`
- `read_only()` classmethod: `svc._ib_client = None` → `svc._massive_client = None`
- **Delete** `_ib_sleep()` method entirely (current lines 265-270)
- **Delete** the 10-min inter-batch pacing sleep in `bulk_refresh` (current line 261-263 block)
- **Delete** the `self._ib_sleep(0.2)` and `self._ib_sleep(backoff_s)` calls in `bulk_refresh` (they're IB-specific)

**(d) Rewrite `bulk_refresh` to return `BulkRefreshResult` and loop both timeframes on `timeframe=None`:**

```python
def bulk_refresh(
    self,
    tickers: list[str],
    timeframe: str | None = None,
) -> BulkRefreshResult:
    """Refresh OHLC + indicators for tickers. Returns counts for caller decisions."""
    if self._massive_client is None:
        logger.warning("bulk_refresh: no Massive client — using cached data only")
        return BulkRefreshResult(attempted=0, succeeded=0, failed=0)

    timeframes = ["1d", "1h"] if timeframe is None else [timeframe]
    attempted = 0
    succeeded = 0
    failed_set: set[str] = set()

    for tf in timeframes:
        stale = [t.upper() for t in tickers if self._is_stale(t.upper(), tf)]
        if not stale:
            logger.info("bulk_refresh: all %d tickers current for %s", len(tickers), tf)
            continue
        logger.info("bulk_refresh: %d/%d tickers need refresh for %s",
                    len(stale), len(tickers), tf)

        for ticker in stale:
            attempted += 1
            try:
                self._refresh(ticker, tf)
                succeeded += 1
            except Exception as exc:
                logger.warning("bulk_refresh: failed %s %s: %s", ticker, tf, exc)
                failed_set.add(ticker)

    failed = attempted - succeeded
    return BulkRefreshResult(
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        failed_tickers=sorted(failed_set),
        aborted=False,
    )
```

**(e) Update `fetch_bars` call-sites** in `_refresh` (current lines 334, 360):

```python
# was: fetch_bars(self._ib_client, ticker, duration=duration, bar_size=bar_size, end_date=end_date)
# now:
df = fetch_bars(
    self._massive_client,
    ticker,
    timeframe=timeframe,
    lookback_days=lookback_days,       # map from old duration logic
    overlap_days=5,
    end_date=end_date,
)
```

Replace IB-style `duration` / `bar_size` mapping with:

```python
_COLD_LOOKBACK_DAYS = {"1d": 365, "1h": 30}

def _compute_lookback_days(self, ticker: str, timeframe: str) -> int:
    latest = get_latest_bar_date(self._conn, ticker, timeframe)
    if latest is None:
        return _COLD_LOOKBACK_DAYS[timeframe]
    days_behind = (_today_et() - latest).days
    return max(days_behind, 1)
```

Where `_today_et()` is an internal helper imported or duplicated from `bars.py`.

**(f) Rewrite `_is_stale()` 1h branch (correct string parsing + `time` import):**

```python
from datetime import time
from scripts.utils.market_calendar import get_last_n_trading_days  # already imported

# inside _is_stale after latest_ts is loaded:
if timeframe == "1h":
    # Prior trading day's last closed RTH hour = 15:30 ET.
    prior_day_strs = get_last_n_trading_days(2)  # e.g. ["2026-04-08", "2026-04-09"]
    if not prior_day_strs:
        return True
    # If len == 1 use that; otherwise use the earlier of the pair (prior trading day).
    target_str = prior_day_strs[-2] if len(prior_day_strs) >= 2 else prior_day_strs[0]
    prior_trading_day = datetime.strptime(target_str, "%Y-%m-%d").date()
    required_min = datetime.combine(prior_trading_day, time(15, 30), tzinfo=_ET)
    return latest_ts < required_min

# 1d branch: unchanged ET-aware session check
```

- [ ] **Step 5: Rewrite `scripts/tests/test_ta_lib/test_bars.py`**

```python
"""Tests for scripts.ta_lib.bars (Massive-backed)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts.ta_lib.bars import fetch_bars


def _massive_df() -> pd.DataFrame:
    return pd.DataFrame([
        {"date": pd.Timestamp("2026-04-08", tz="America/New_York"),
         "open": 170.0, "high": 172.0, "low": 169.5, "close": 171.5,
         "volume": 50_000_000, "vwap": 170.8, "tx_count": 450_000},
        {"date": pd.Timestamp("2026-04-09", tz="America/New_York"),
         "open": 171.5, "high": 173.0, "low": 170.9, "close": 172.7,
         "volume": 48_000_000, "vwap": 171.9, "tx_count": 430_000},
    ])


def test_fetch_bars_returns_store_compatible_columns():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    df = fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=30)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]


def test_fetch_bars_passes_overlap_days():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    with patch("scripts.ta_lib.bars._today_et", return_value=date(2026, 4, 10)):
        fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=30, overlap_days=5)
    kwargs = mc.get_aggregates.call_args.kwargs
    # to_date = today ET; from_date = to_date - (30 + 5) = 35 days back
    assert kwargs["to_date"] == "2026-04-10"
    assert kwargs["from_date"] == "2026-03-06"


def test_fetch_bars_1h_timeframe():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    fetch_bars(mc, "AAPL", timeframe="1h", lookback_days=7)
    assert mc.get_aggregates.call_args.kwargs["timeframe"] == "1h"


def test_fetch_bars_filters_malformed_rows():
    bad = _massive_df().copy()
    bad.loc[len(bad)] = {
        "date": pd.Timestamp("2026-04-10", tz="America/New_York"),
        "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0,
        "volume": -1, "vwap": 0.0, "tx_count": 0,
    }
    mc = MagicMock()
    mc.get_aggregates.return_value = bad
    df = fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=30)
    assert len(df) == 2


def test_fetch_bars_invalid_timeframe():
    with pytest.raises(ValueError):
        fetch_bars(MagicMock(), "AAPL", timeframe="5m", lookback_days=30)


def test_fetch_bars_defaults_to_ET_today():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    with patch("scripts.ta_lib.bars._today_et", return_value=date(2026, 4, 15)):
        fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=10)
    assert mc.get_aggregates.call_args.kwargs["to_date"] == "2026-04-15"
```

- [ ] **Step 6: Update `scripts/tests/test_ta_lib/test_service.py`**

Global edit: replace every `ib_client=...` construction with `massive_client=...`. Replace IB mocks with:

```python
def _massive_mock_ok():
    """Returns a DataFrame shaped like MassiveClient.get_aggregates output."""
    mc = MagicMock()
    mc.get_aggregates.return_value = pd.DataFrame([{
        "date": pd.Timestamp("2026-04-09", tz="America/New_York"),
        "open": 170.0, "high": 172.0, "low": 169.5, "close": 171.5,
        "volume": 50_000_000, "vwap": 170.8, "tx_count": 450_000,
    }])
    return mc
```

Add new test classes:

```python
class TestBulkRefreshResult:
    def test_returns_counts_on_success(self, tmp_path):
        mc = _massive_mock_ok()
        svc = TAService(db_path=str(tmp_path / "ta.duckdb"), massive_client=mc)
        with patch.object(svc, "_is_stale", return_value=True), \
             patch.object(svc, "_refresh"):
            result = svc.bulk_refresh(["AAPL", "SPY"], timeframe="1d")
        assert result.attempted == 2
        assert result.succeeded == 2
        assert result.failed == 0
        assert result.failure_ratio == 0.0

    def test_failure_ratio_on_partial(self, tmp_path):
        mc = _massive_mock_ok()
        svc = TAService(db_path=str(tmp_path / "ta.duckdb"), massive_client=mc)
        def _refresh_side_effect(ticker, tf):
            if ticker == "BAD":
                raise RuntimeError("simulated")
        with patch.object(svc, "_is_stale", return_value=True), \
             patch.object(svc, "_refresh", side_effect=_refresh_side_effect):
            result = svc.bulk_refresh(["AAPL", "BAD", "SPY"], timeframe="1d")
        assert result.attempted == 3
        assert result.succeeded == 2
        assert result.failed == 1
        assert result.failed_tickers == ["BAD"]
        assert 0.33 < result.failure_ratio < 0.34

    def test_loops_both_timeframes_when_none(self, tmp_path):
        mc = _massive_mock_ok()
        svc = TAService(db_path=str(tmp_path / "ta.duckdb"), massive_client=mc)
        seen: list[str] = []
        def _refresh(ticker, tf):
            seen.append(tf)
        with patch.object(svc, "_is_stale", return_value=True), \
             patch.object(svc, "_refresh", side_effect=_refresh):
            svc.bulk_refresh(["AAPL"])  # no timeframe — loops both
        assert set(seen) == {"1d", "1h"}


class TestIsStale1hNewContract:
    """Patch both datetime.now and get_last_n_trading_days so the 1h branch is reached
    deterministically. Also seed the cache so _is_stale gets past the `latest is None` check."""

    @staticmethod
    def _make_svc_with_row(tmp_path):
        from scripts.ta_lib.store import init_schema, write_ohlc, get_connection
        db = tmp_path / "ta.duckdb"
        conn = get_connection(str(db))
        init_schema(conn)
        # seed one row so _is_stale reaches the freshness check
        row = pd.DataFrame([{
            "date": pd.Timestamp("2026-04-08 15:30", tz="America/New_York"),
            "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
        }])
        write_ohlc(conn, "AAPL", "1h", row)
        conn.commit()
        svc = TAService(db_path=str(db), massive_client=MagicMock())
        return svc

    def test_fresh_when_latest_is_prior_1530(self, tmp_path, monkeypatch):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        frozen_now = datetime(2026, 4, 9, 11, 0, tzinfo=et)
        monkeypatch.setattr(
            "scripts.ta_lib.service.datetime",
            MagicMock(now=lambda tz=None: frozen_now,
                      combine=datetime.combine,
                      strptime=datetime.strptime),
        )
        monkeypatch.setattr(
            "scripts.ta_lib.service.get_last_n_trading_days",
            lambda n: ["2026-04-08", "2026-04-09"],
        )
        svc = self._make_svc_with_row(tmp_path)
        with patch(
            "scripts.ta_lib.service.read_indicators",
            return_value=pd.DataFrame({"x": [1]}),
        ):
            assert svc._is_stale("AAPL", "1h") is False

    def test_stale_when_multiple_days_behind(self, tmp_path, monkeypatch):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        frozen_now = datetime(2026, 4, 15, 11, 0, tzinfo=et)
        monkeypatch.setattr(
            "scripts.ta_lib.service.datetime",
            MagicMock(now=lambda tz=None: frozen_now,
                      combine=datetime.combine,
                      strptime=datetime.strptime),
        )
        monkeypatch.setattr(
            "scripts.ta_lib.service.get_last_n_trading_days",
            lambda n: ["2026-04-14", "2026-04-15"],
        )
        svc = self._make_svc_with_row(tmp_path)  # seeded with 2026-04-08 15:30
        with patch(
            "scripts.ta_lib.service.read_indicators",
            return_value=pd.DataFrame({"x": [1]}),
        ):
            assert svc._is_stale("AAPL", "1h") is True
```

- [ ] **Step 7: Update `scripts/tests/test_ta_lib/test_snapshot_contract.py`**

Replace every `ib_client=` with `massive_client=`. For the `read_only()` tests, no change needed — `read_only()` doesn't take a client.

- [ ] **Step 8: Run all affected tests — should pass**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_bars.py scripts/tests/test_ta_lib/test_service.py scripts/tests/test_ta_lib/test_snapshot_contract.py -xvs`
Expected: all pass.

- [ ] **Step 9: Commit (single atomic commit spanning all files)**

```bash
git add scripts/ta_lib/bars.py scripts/ta_lib/service.py scripts/tests/test_ta_lib/
git commit -m "refactor(ta_lib): Massive-backed bars + TAService with BulkRefreshResult"
```

---

## Task 4: `ta_premarket_prep` — lazy Massive, both timeframes, status file, exit code 3

**Files:**

- Modify: `scripts/ta_premarket_prep.py`
- Modify: `scripts/tests/test_ta_lib/test_premarket_prep.py`

- [ ] **Step 1: Change `main()` return type and add `import os` to test file**

First, modify `scripts/ta_premarket_prep.py` line 180:

```python
# Before:  def main(argv: Optional[list[str]] = None) -> None:
# After:
def main(argv: Optional[list[str]] = None) -> int:
    ...
    # every existing "return" that currently returns None: change to "return 0"
```

And the bottom of the file:

```python
if __name__ == "__main__":
    sys.exit(main() or 0)
```

Add `import os` at the top of `scripts/tests/test_ta_lib/test_premarket_prep.py` (needed for `@patch.dict(os.environ, ...)`).

- [ ] **Step 2: Write failing tests**

Append to `scripts/tests/test_ta_lib/test_premarket_prep.py`:

```python
import os  # noqa: E402 — add to imports list at top

from scripts.ta_lib.service import BulkRefreshResult


@patch.dict(os.environ, {}, clear=True)
def test_audit_only_works_without_massive_key(monkeypatch):
    """Regression: --audit-only must not touch network or API key."""
    import scripts.ta_premarket_prep as prep
    monkeypatch.setattr(
        prep, "_build_static_only_universe", lambda args: ["AAPL", "SPY"]
    )
    with patch("scripts.ta_premarket_prep.get_latest_bar_date",
               return_value=date(2026, 4, 10)), \
         patch("scripts.ta_premarket_prep._last_trading_date",
               return_value=date(2026, 4, 10)), \
         patch("scripts.ta_premarket_prep.init_schema"), \
         patch("scripts.ta_premarket_prep.get_connection"):
        exit_code = prep.main(["--audit-only"])
    assert exit_code == 0


def test_vendor_down_emits_exit_code_3_and_status_file(tmp_path, monkeypatch):
    """>50% failure ratio → exit 3, aborted status file."""
    import scripts.ta_premarket_prep as prep

    # bulk_refresh reports real counts now (not None)
    fake_svc = MagicMock()
    fake_svc.bulk_refresh.return_value = BulkRefreshResult(
        attempted=4, succeeded=1, failed=3,
        failed_tickers=["SPY", "MSFT", "TSLA"], aborted=False,
    )
    monkeypatch.setattr(prep, "TAService", lambda **kw: fake_svc)
    monkeypatch.setattr(prep, "_build_triple_source_universe",
                        lambda *a, **kw: (["AAPL", "SPY", "MSFT", "TSLA"], None, None, False))
    monkeypatch.setattr(prep, "STATUS_FILE", tmp_path / "status.json")
    # Bypass client construction — we patched TAService to a fake anyway:
    monkeypatch.setattr(prep, "_build_massive_client", lambda: MagicMock())

    with patch("scripts.ta_premarket_prep.get_latest_bar_date",
               return_value=date(2026, 4, 1)), \
         patch("scripts.ta_premarket_prep._last_trading_date",
               return_value=date(2026, 4, 14)), \
         patch("scripts.ta_premarket_prep.init_schema"), \
         patch("scripts.ta_premarket_prep.get_connection"):
        exit_code = prep.main([])
    assert exit_code == 3

    from scripts.api_status.ta_premarket_status import load_status
    status = load_status(tmp_path / "status.json")
    assert status is not None
    assert status.status == "aborted"
    assert status.failure_reason == "vendor_down"
    assert status.failed_tickers == 3
    assert status.refreshed_tickers == 1


def test_success_writes_ok_status(tmp_path, monkeypatch):
    import scripts.ta_premarket_prep as prep

    fake_svc = MagicMock()
    fake_svc.bulk_refresh.return_value = BulkRefreshResult(
        attempted=2, succeeded=2, failed=0, failed_tickers=[], aborted=False,
    )
    monkeypatch.setattr(prep, "TAService", lambda **kw: fake_svc)
    monkeypatch.setattr(prep, "_build_triple_source_universe",
                        lambda *a, **kw: (["AAPL", "SPY"], None, None, False))
    monkeypatch.setattr(prep, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(prep, "_build_massive_client", lambda: MagicMock())

    with patch("scripts.ta_premarket_prep.get_latest_bar_date",
               return_value=date(2026, 4, 1)), \
         patch("scripts.ta_premarket_prep._last_trading_date",
               return_value=date(2026, 4, 14)), \
         patch("scripts.ta_premarket_prep.init_schema"), \
         patch("scripts.ta_premarket_prep.get_connection"):
        exit_code = prep.main([])
    assert exit_code == 0

    from scripts.api_status.ta_premarket_status import load_status
    status = load_status(tmp_path / "status.json")
    assert status.status == "ok"
```

- [ ] **Step 3: Run — confirm failures**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_premarket_prep.py -xvs`
Expected: new tests fail.

- [ ] **Step 4: Modify `scripts/ta_premarket_prep.py`**

**(a) Add imports + module constants near the top (after existing imports):**

```python
from pathlib import Path

from scripts.api_status.ta_premarket_status import TaPremarketStatus, save_status

STATUS_FILE = Path("data/ta_premarket_status.json")
EXIT_VENDOR_DOWN = 3  # distinct from pytest's collection-error 2
_FAILURE_ABORT_RATIO = 0.50


def _build_massive_client():
    """Factory — kept as a module-level function so tests can monkeypatch it."""
    from scripts.clients.massive_client import MassiveClient
    return MassiveClient()
```

**(b) Replace the refresh branch of `main()` (starting at the current `# Refresh mode` comment around line 203). Keep the universe building (`_build_triple_source_universe`) and `classify_tickers` unchanged — they don't touch Massive. Pre-compute `refresh_tickers` BEFORE any client construction:**

```python
# Refresh mode: build the FULL scanner universe (unchanged)
cfg = TrendScanConfig(sp500_path=args.sp500, nasdaq100_path=args.nasdaq100)
tickers, uw_client, ib_client_preopened, used_triple_source = _build_triple_source_universe(cfg)

# Persist universe cache (unchanged) ...

before = classify_tickers(conn, tickers, ref_date)
_print_audit("BEFORE", before)

# Compute refresh set BEFORE building any Massive client (C2 fix)
refresh_tickers = tickers if args.force else before["stale"] + before["missing"]
total = len(refresh_tickers)

if not refresh_tickers:
    logger.info("No tickers to refresh.")
    save_status(STATUS_FILE, TaPremarketStatus(
        last_run_ts=datetime.now(timezone.utc),
        status="ok", refreshed_tickers=0, failed_tickers=0,
        failure_ratio=0.0, failure_reason=None,
    ))
    return 0

massive = _build_massive_client()
ta_svc = TAService(db_path=args.db, massive_client=massive)

t0 = time.monotonic()
try:
    result = ta_svc.bulk_refresh(refresh_tickers)  # loops both 1d + 1h
except Exception as exc:
    logger.exception("bulk_refresh crashed outright: %s", exc)
    save_status(STATUS_FILE, TaPremarketStatus(
        last_run_ts=datetime.now(timezone.utc),
        status="aborted", refreshed_tickers=0, failed_tickers=total,
        failure_ratio=1.0, failure_reason="vendor_down",
    ))
    return EXIT_VENDOR_DOWN

ratio = result.failure_ratio
if ratio >= _FAILURE_ABORT_RATIO:
    save_status(STATUS_FILE, TaPremarketStatus(
        last_run_ts=datetime.now(timezone.utc),
        status="aborted",
        refreshed_tickers=result.succeeded,
        failed_tickers=result.failed,
        failure_ratio=ratio,
        failure_reason="vendor_down",
    ))
    return EXIT_VENDOR_DOWN

status_kind = "ok" if result.failed == 0 else "partial"
save_status(STATUS_FILE, TaPremarketStatus(
    last_run_ts=datetime.now(timezone.utc),
    status=status_kind,
    refreshed_tickers=result.succeeded,
    failed_tickers=result.failed,
    failure_ratio=ratio,
    failure_reason=None,
))

elapsed = time.monotonic() - t0
after = classify_tickers(conn, tickers, ref_date)
_print_audit("AFTER", after)
# (existing JSON output block — retained; add "result" counts if desired)
return 0
```

Note: `_build_triple_source_universe` already returns a tuple `(tickers, uw_client, ib_client_preopened, used_triple_source)` — the plan's test mocks match this tuple shape.

**(c) Update the `__main__` block at the end of the file:**

```python
if __name__ == "__main__":
    sys.exit(main() or 0)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_premarket_prep.py -xvs`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/ta_premarket_prep.py scripts/tests/test_ta_lib/test_premarket_prep.py
git commit -m "feat(ta_premarket_prep): Massive + status file + exit 3 on vendor down"
```

---

## Task 5: `ta_cli` — lazy Massive, preserve normal-read behavior

> **Fix C9:** Construct client when not `--cache-only`, not only on `--refresh`. Normal `ta-cli AAPL` currently falls through `allow_fetch=True` to the cache; this must remain possible.
> **Fix C8:** Patch what actually exists in `ta_cli`. Do not patch `scripts.ta_cli.get_connection` (doesn't exist there).

**Files:**

- Modify: `scripts/ta_cli.py`
- Create: `scripts/tests/test_ta_lib/test_ta_cli_offline.py`

- [ ] **Step 1: Inspect current ta_cli import structure**

Run: `grep -n "import\|def main\|TAService\|get_connection" scripts/ta_cli.py | head -25`

Note which symbols are imported directly into `ta_cli.py`'s namespace (these are what tests can patch).

- [ ] **Step 2: Write failing offline tests**

Create `scripts/tests/test_ta_lib/test_ta_cli_offline.py`:

```python
"""Regression: ta-cli offline subcommands must not need MASSIVE_API_KEY."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import scripts.ta_cli as cli


@pytest.mark.parametrize("argv", [
    ["--stats"],
    ["--query", "SELECT 1"],
    ["--cache-only", "AAPL"],
])
@patch.dict(os.environ, {}, clear=True)
def test_offline_subcommands_dont_need_api_key(argv, monkeypatch):
    """These must not raise MassiveAuthError."""
    # Patch TAService construction at the `ta_cli` namespace (wherever it's imported).
    fake_svc = MagicMock()
    fake_svc.get_snapshot.return_value = {"ticker": "AAPL", "close": 1.0}
    fake_svc.get_indicators.return_value = {"ma_20": 1.0}
    monkeypatch.setattr(cli, "TAService", lambda **kw: fake_svc)
    # Patch the client factory we're about to add, too:
    monkeypatch.setattr(cli, "_build_massive_client", lambda: None)
    try:
        cli.main(argv)
    except SystemExit as e:
        # CLI may choose to sys.exit(0); that's fine.
        assert e.code in (None, 0)
```

- [ ] **Step 3: Run — confirm fail**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_ta_cli_offline.py -xvs`
Expected: `MassiveAuthError` raised due to eager construction (or `AttributeError` for `_build_massive_client`).

- [ ] **Step 4: Refactor `scripts/ta_cli.py`**

Introduce a factory and move client construction past the offline subcommands:

```python
def _build_massive_client():
    """Factory — monkeypatchable for tests."""
    from scripts.clients.massive_client import MassiveClient
    return MassiveClient()


def main(argv=None) -> int:
    args = _parse_args(argv)

    # --- Offline subcommands first (no client needed) ---
    if args.stats:
        return _cmd_stats(args)
    if args.query:
        return _cmd_query(args)

    # --- Reads: client needed unless --cache-only ---
    client = None if args.cache_only else _build_massive_client()
    svc = TAService(db_path=args.db, massive_client=client)
    if args.refresh:
        svc.bulk_refresh(args.tickers, timeframe=args.timeframe)
    for ticker in args.tickers:
        snap = svc.get_snapshot(ticker, args.timeframe, allow_fetch=not args.cache_only)
        _print(snap)
    return 0
```

Keep the existing `--history`, `--stats`, `--query`, `--cache-only`, `--refresh` args. Adapt the above skeleton to the actual current argparse structure — don't discard existing features.

- [ ] **Step 5: Run tests, all pass**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_ta_cli_offline.py scripts/tests/test_ta_lib/ -xvs`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/ta_cli.py scripts/tests/test_ta_lib/test_ta_cli_offline.py
git commit -m "feat(ta_cli): lazy MassiveClient — offline subcommands skip API"
```

---

## Task 6: `trend_scan.py` — update TAService instantiation + read status file + warnings output

**Files:**

- Modify: `scripts/trend_scan.py` (around line 803)
- Modify: `scripts/tests/` — add unit test for status-warning propagation

- [ ] **Step 1: Write failing test**

Create or append to a trend_scan test file (e.g., `scripts/tests/test_trend_scan_status.py`):

```python
"""Trend scan surfaces a warning when ta_premarket_status.json indicates abort."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from scripts.api_status.ta_premarket_status import TaPremarketStatus, save_status


def test_trend_scan_warns_on_aborted_status(tmp_path):
    status_path = tmp_path / "ta_premarket_status.json"
    save_status(status_path, TaPremarketStatus(
        last_run_ts=datetime(2026, 4, 15, 6, 5, tzinfo=timezone.utc),
        status="aborted",
        refreshed_tickers=10,
        failed_tickers=200,
        failure_ratio=0.95,
        failure_reason="vendor_down",
    ))
    with patch("scripts.trend_scan.STATUS_FILE", status_path):
        from scripts.trend_scan import _check_premarket_status
        warn = _check_premarket_status()
    assert warn is not None
    assert "aborted" in warn.lower() or "vendor_down" in warn.lower()


def test_trend_scan_warns_on_stale_status_file_age(tmp_path):
    """Even status=ok but file older than 1 trading day should warn."""
    from datetime import timedelta
    status_path = tmp_path / "ta_premarket_status.json"
    # File timestamp 3 days old, marked ok
    save_status(status_path, TaPremarketStatus(
        last_run_ts=datetime.now(timezone.utc) - timedelta(days=3),
        status="ok", refreshed_tickers=250, failed_tickers=0,
        failure_ratio=0.0, failure_reason=None,
    ))
    with patch("scripts.trend_scan.STATUS_FILE", status_path):
        from scripts.trend_scan import _check_premarket_status
        warn = _check_premarket_status()
    assert warn is not None
    assert "stale" in warn.lower() or "old" in warn.lower()


def test_trend_scan_no_warning_when_fresh_ok(tmp_path):
    save_status(tmp_path / "ta_premarket_status.json", TaPremarketStatus(
        last_run_ts=datetime.now(timezone.utc),
        status="ok", refreshed_tickers=250, failed_tickers=0,
        failure_ratio=0.0, failure_reason=None,
    ))
    with patch("scripts.trend_scan.STATUS_FILE", tmp_path / "ta_premarket_status.json"):
        from scripts.trend_scan import _check_premarket_status
        assert _check_premarket_status() is None


def test_trend_scan_output_includes_warnings_field(tmp_path, monkeypatch):
    """The output dict written to data/trend_scan.json must include a 'warnings' list."""
    from scripts.trend_scan import build_scan_output  # or whatever the output builder is
    # Assemble a minimal fake output and assert the contract.
    out = build_scan_output(candidates=[], warnings=["test warning"])
    assert "warnings" in out
    assert out["warnings"] == ["test warning"]
```

- [ ] **Step 2: Run, confirm fail**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_status.py -xvs`
Expected: `ImportError` or `AttributeError` for `_check_premarket_status` / `STATUS_FILE`.

- [ ] **Step 3: Modify `scripts/trend_scan.py`**

Add near other constants/imports:

```python
from datetime import timedelta, timezone as _tz
from pathlib import Path
from scripts.api_status.ta_premarket_status import load_status

STATUS_FILE = Path("data/ta_premarket_status.json")
_STATUS_STALE_AFTER = timedelta(days=1)  # 1 calendar day; good-enough proxy for "missed run"


def _check_premarket_status() -> str | None:
    """Return a human-readable warning if status indicates abort or is stale, else None."""
    status = load_status(STATUS_FILE)
    if status is None:
        return None  # first-run case; nothing to warn about yet
    if status.status == "aborted":
        return (
            f"Premarket prep aborted at {status.last_run_ts.isoformat()} "
            f"(reason: {status.failure_reason}, {status.failed_tickers}/"
            f"{status.refreshed_tickers + status.failed_tickers} tickers failed). "
            "Cache may be stale."
        )
    age = datetime.now(_tz.utc) - status.last_run_ts
    if age > _STATUS_STALE_AFTER:
        return (
            f"Premarket prep status is stale (last_run={status.last_run_ts.isoformat()}, "
            f"age={age}). Cache may be old."
        )
    return None
```

**Add `build_scan_output()` helper if one doesn't already exist.** The current output dict lives inline around `scripts/trend_scan.py:715-724`. Extract it to a helper so tests can exercise the contract:

```python
def build_scan_output(
    *,
    candidates: list[dict],
    warnings: list[str] | None = None,
    scan_id: str | None = None,
    scan_timestamp: str | None = None,
    market_context: dict | None = None,
) -> dict:
    return {
        "scan_id": scan_id or _make_scan_id(),
        "scan_timestamp": scan_timestamp or datetime.now(_tz.utc).isoformat(timespec="seconds"),
        "market_context": market_context or {},
        "candidates": candidates,
        "counts": {"total_candidates": len(candidates)},
        "warnings": warnings or [],
    }
```

Then rewrite the inline `output = {...}` block to call `build_scan_output(candidates=..., warnings=scan_warnings, ...)`.

At line 803 (TAService construction), change:

```python
# Before:
#   ta_service = TAService(db_path="data/ta.duckdb", ib_client=ib_client)
# After:
scan_warnings: list[str] = []
try:
    from scripts.clients.massive_client import MassiveClient
    massive = MassiveClient()
except Exception as exc:
    logger.warning("MassiveClient unavailable — cache will use stale data: %s", exc)
    massive = None
    scan_warnings.append(f"MassiveClient unavailable: {exc}")

ta_service = TAService(db_path="data/ta.duckdb", massive_client=massive)

warning = _check_premarket_status()
if warning:
    logger.warning(warning)
    scan_warnings.append(warning)

# ...later, when assembling output:
output = build_scan_output(candidates=candidates, warnings=scan_warnings, ...)
```

This wires `scan_warnings` through `build_scan_output` into the existing `data/trend_scan.json` writer — no more TODO.

- [ ] **Step 4: Run test, verify pass**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_status.py -xvs`
Expected: pass.

- [ ] **Step 5: Run broader regression**

Run: `python3.13 -m pytest scripts/tests/ -xvs -k "trend_scan or ta_"`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/trend_scan.py scripts/tests/test_trend_scan_status.py
git commit -m "feat(trend_scan): surface ta_premarket_status abort warnings"
```

---

## Task 7: Add `e2e` pytest marker + E2E test scaffold

**Files:**

- Modify: `pyproject.toml`
- Create: `scripts/tests/test_e2e_massive_pipeline.py`

- [ ] **Step 1: Add `e2e` marker to `pyproject.toml`**

In `[tool.pytest.ini_options]`, extend `markers`:

```toml
markers = [
    "integration: live tests hitting real MenthorQ (requires credentials)",
    "e2e: live tests hitting real Massive API (requires MASSIVE_API_KEY)",
]
```

- [ ] **Step 2: Create E2E test file**

```python
"""E2E tests — opt-in (pytest -m e2e). Requires MASSIVE_API_KEY."""

from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

if not os.environ.get("MASSIVE_API_KEY"):
    pytest.skip("MASSIVE_API_KEY not set — skipping E2E", allow_module_level=True)


def test_e2e_1_smoke_aapl_30d():
    from scripts.clients.massive_client import MassiveClient
    mc = MassiveClient()
    to_d = date.today()
    from_d = to_d - timedelta(days=30)
    df = mc.get_aggregates("AAPL", "1d", from_d.isoformat(), to_d.isoformat())
    assert len(df) >= 18  # at least ~18 trading days in 30 calendar days
    assert df["date"].is_monotonic_increasing
    assert (df["open"] > 0).all()
    assert (df["close"] > 0).all()
    assert not df["close"].isna().any()


def test_e2e_2_full_pipeline_mini_universe():
    """Persistence-level assertion using read_ohlc (not get_snapshot which returns dict)."""
    from scripts.clients.massive_client import MassiveClient
    from scripts.ta_lib.service import TAService
    from scripts.ta_lib.store import read_ohlc
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "ta.duckdb"
        svc = TAService(db_path=str(db), massive_client=MassiveClient())
        result = svc.bulk_refresh(["AAPL", "TSLA", "SPY", "MSFT", "NVDA"])
        assert result.succeeded >= 4, f"too many failures: {result.failed_tickers}"
        for t in ["AAPL", "TSLA", "SPY", "MSFT", "NVDA"]:
            for tf in ("1d", "1h"):
                df = read_ohlc(svc._conn, t, tf)
                assert df is not None and len(df) > 0, f"{t} {tf} has no persisted rows"
                assert {"open", "high", "low", "close", "volume"}.issubset(df.columns)


@pytest.mark.skipif(
    not os.environ.get("XENON_ENABLE_IB_E2E"),
    reason="IB E2E requires running IB Gateway; set XENON_ENABLE_IB_E2E=1",
)
def test_e2e_3_vendor_divergence_ib_vs_massive():
    """Close-price divergence < 0.5% on last 5 daily bars for AAPL, SPY, TSLA."""
    from scripts.clients.ib_client import IBClient
    from scripts.clients.massive_client import MassiveClient
    from scripts.ta_lib.bars import fetch_bars as fetch_massive

    ib = IBClient()
    ib.connect(client_id="auto")
    massive = MassiveClient()
    try:
        for ticker in ("AAPL", "SPY", "TSLA"):
            m = fetch_massive(massive, ticker, timeframe="1d", lookback_days=7).tail(5)
            from ib_insync import Stock
            contract = ib._ib.qualifyContracts(Stock(ticker, "SMART", "USD"))[0]
            bars = ib.get_historical_data(
                contract=contract, duration="7 D", bar_size="1 day",
                what_to_show="TRADES", end_date="",
            )
            ib_closes = [float(b.close) for b in bars[-5:]]
            m_closes = m["close"].tolist()
            for i, (ibc, mc) in enumerate(zip(ib_closes, m_closes)):
                div = abs(ibc - mc) / ibc
                assert div < 0.005, (
                    f"{ticker} bar {i} divergence {div:.4%} "
                    f"(IB={ibc}, Massive={mc})"
                )
    finally:
        ib.disconnect()
```

- [ ] **Step 3: Verify marker registration**

Run: `python3.13 -m pytest --markers | grep e2e`
Expected: `@pytest.mark.e2e: live tests hitting real Massive API (requires MASSIVE_API_KEY)`

- [ ] **Step 4: Default run excludes E2E**

Run: `python3.13 -m pytest scripts/tests/test_e2e_massive_pipeline.py -m "not e2e" --collect-only`
Expected: no tests collected.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml scripts/tests/test_e2e_massive_pipeline.py
git commit -m "test: e2e pytest marker + Massive E2E scaffold"
```

---

## Task 8: `scripts/ta_reseed_massive.py` operator script

**Files:**

- Create: `scripts/ta_reseed_massive.py`
- Create: `scripts/tests/test_ta_reseed_massive.py` (smoke only — mocked Massive)

- [ ] **Step 1: Write smoke test**

```python
"""Smoke test for ta_reseed_massive — mocked Massive, real DuckDB."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def _mock_df():
    return pd.DataFrame(
        [{"date": pd.Timestamp("2026-04-08", tz="America/New_York"),
          "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
          "volume": 1000, "vwap": 1.0, "tx_count": 10}]
    )


def test_reseed_loops_timeframes_and_uses_lookback(tmp_path):
    mc = MagicMock()
    mc.get_aggregates.return_value = _mock_df()

    with patch("scripts.ta_reseed_massive.MassiveClient", return_value=mc), \
         patch("scripts.ta_reseed_massive.compute_all", return_value=_mock_df().assign(bar_date=lambda d: d["date"])):
        import scripts.ta_reseed_massive as reseed
        exit_code = reseed.main([
            "--db", str(tmp_path / "ta.duckdb"),
            "--tickers", "AAPL,SPY",
            "--timeframes", "1d,1h",
            "--lookback-days", "30",
        ])
    assert exit_code == 0
    # 2 tickers × 2 timeframes = 4 fetches
    assert mc.get_aggregates.call_count == 4
    # --lookback-days must flow through
    for call in mc.get_aggregates.call_args_list:
        assert "from_date" in call.kwargs
```

- [ ] **Step 2: Implement `scripts/ta_reseed_massive.py`**

```python
"""Bulk re-seed the TA DuckDB cache from Massive. Replaces ta_seed_yahoo.py.

Usage:
  python3.13 scripts/ta_reseed_massive.py \\
      --timeframes 1d,1h --lookback-days 730 --tickers AAPL,SPY
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from scripts.clients.massive_client import MassiveClient, MassiveError
from scripts.ta_lib.service import TAService

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Re-seed TA DuckDB from Massive")
    p.add_argument("--db", default="data/ta.duckdb")
    p.add_argument("--timeframes", default="1d,1h",
                   help="Comma-separated. Supported: 1d, 1h")
    p.add_argument("--lookback-days", type=int, default=730,
                   help="Calendar days of history to request")
    p.add_argument("--tickers", default="",
                   help="Comma-separated override. Default uses trend-scan universe.")
    p.add_argument("--sp500", default="data/universe/sp500.json")
    p.add_argument("--nasdaq100", default="data/universe/nasdaq100.json")
    p.add_argument("--dry-run", action="store_true",
                   help="Don't write; just list what would fetch")
    return p.parse_args(list(argv))


def _resolve_universe(override: str, sp500_path: str, nasdaq100_path: str) -> list[str]:
    if override:
        return [t.strip().upper() for t in override.split(",") if t.strip()]
    # build_static_universe requires keyword args with explicit paths.
    from scripts.trend_scan_lib.universe import build_static_universe
    return build_static_universe(sp500_path=sp500_path, nasdaq100_path=nasdaq100_path)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    tickers = _resolve_universe(args.tickers, args.sp500, args.nasdaq100)
    logger.info(
        "Re-seeding %d tickers × %d timeframes from Massive (lookback=%dd)",
        len(tickers), len(timeframes), args.lookback_days,
    )

    if args.dry_run:
        for t in tickers:
            for tf in timeframes:
                logger.info("DRY: would fetch %s %s lookback=%dd", t, tf, args.lookback_days)
        return 0

    # Build DuckDB connection directly — we call fetch_bars + write_ohlc + indicators
    # so --lookback-days is honored (TAService.bulk_refresh uses its own cold window).
    from scripts.ta_lib.bars import fetch_bars
    from scripts.ta_lib.indicators import compute_all
    from scripts.ta_lib.store import get_connection, init_schema, write_indicators, write_ohlc

    massive = MassiveClient()
    conn = get_connection(args.db)
    init_schema(conn)

    failed: list[tuple[str, str, str]] = []
    for t in tickers:
        for tf in timeframes:
            try:
                df = fetch_bars(massive, t, timeframe=tf,
                                lookback_days=args.lookback_days, overlap_days=0)
                if df.empty:
                    raise RuntimeError("empty frame")
                write_ohlc(conn, t, tf, df)
                indicators = compute_all(df)
                indicators["bar_date"] = indicators["date"]
                write_indicators(conn, t, tf, indicators)
                conn.commit()
            except MassiveError as exc:
                logger.warning("skip %s %s: %s", t, tf, exc)
                failed.append((t, tf, str(exc)))
            except Exception as exc:
                conn.rollback()
                logger.warning("error %s %s: %s", t, tf, exc)
                failed.append((t, tf, str(exc)))

    logger.info("Done. %d failures.", len(failed))
    if failed:
        logger.warning("First 10 failed entries: %s", failed[:10])
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run smoke test**

Run: `python3.13 -m pytest scripts/tests/test_ta_reseed_massive.py -xvs`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/ta_reseed_massive.py scripts/tests/test_ta_reseed_massive.py
git commit -m "feat(ta_lib): ta_reseed_massive.py operator script"
```

---

## Task 9: Delete `ta_seed_yahoo.py` + tests

**Files:**

- Delete: `scripts/ta_seed_yahoo.py`
- Delete: `scripts/tests/test_ta_lib/test_seed_yahoo.py`

- [ ] **Step 1: Verify no other references**

Run: `grep -rn "ta_seed_yahoo\|yfinance" scripts/ docs/ CHANGELOG.md | grep -v -e ".pyc" -e "docs/superpowers/specs/2026-04-13" -e "docs/superpowers/plans/2026-04-15" -e "docs/superpowers/specs/2026-04-15" -e "CHANGELOG"`
Expected: no matches outside historical docs / the migration spec itself.

- [ ] **Step 2: Delete files**

```bash
git rm scripts/ta_seed_yahoo.py scripts/tests/test_ta_lib/test_seed_yahoo.py
```

- [ ] **Step 3: Verify test suite still passes**

Run: `python3.13 -m pytest scripts/tests/ -xvs -k "not e2e"`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore(ta_lib): remove ta_seed_yahoo — Massive handles seeding now"
```

---

## Task 10: Update `CLAUDE.md`, `scripts/CLAUDE.md`, `CHANGELOG.md`

**Files:**

- Modify: `CLAUDE.md` (root)
- Modify: `scripts/CLAUDE.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Amend root `CLAUDE.md` Data Source Priority**

Replace the Data Source Priority block with:

```
## Data Source Priority

1. Interactive Brokers — real-time quotes, chains, execution, live portfolio
2. Massive.com — historical OHLCV (≤ yesterday EOD), 1d + 1h bars
3. Unusual Whales ($UW_TOKEN) — dark pool, sweeps, alerts
4. Web scrape — last resort

Never use Yahoo Finance. Historical data is sourced exclusively from Massive.
```

Add `MASSIVE_API_KEY` to the credentials table under root `.env`.

- [ ] **Step 2: Amend `scripts/CLAUDE.md`**

Replace the `ta_lib/` bullet in "Scanner Libs — Shared Foundation":

```
- `scripts/ta_lib/` — TA-Lib indicators with Massive-backed historical bars and DuckDB caching. `bars.py` (Massive OHLC fetch), `indicators.py`, `store.py` (DuckDB at `data/ta.duckdb`), `service.py` (TAService read-through cache). Supports 1d and 1h. Re-seed utility: `ta_reseed_massive.py`. Status file: `data/ta_premarket_status.json`. Design spec: `docs/superpowers/specs/2026-04-15-massive-historical-data-migration-design.md`.
```

Remove `ta-seed-yahoo` row from commands table; add:

```
| `ta-reseed-massive [TICKERS]` | Bulk re-seed DuckDB from Massive. `--timeframes 1d,1h` `--lookback-days 730` `--dry-run` |
```

- [ ] **Step 3: Add `CHANGELOG.md` entry**

Under the next version (likely `## 0.2.0` for this migration-scale change):

```
### Changed
- **Historical data source**: swapped IB for Massive.com for all OHLCV backfills
  (1d, 1h). IB retained for live quotes/chains/execution only. `ta_seed_yahoo`
  retired; `ta_reseed_massive` handles seeds. Spec:
  `docs/superpowers/specs/2026-04-15-massive-historical-data-migration-design.md`.

### Added
- `MassiveClient` (`scripts/clients/massive_client.py`)
- `ta_premarket_status.json` — machine-readable prep outcome consumed by trend_scan
- `e2e` pytest marker for Massive-live tests

### Removed
- `scripts/ta_seed_yahoo.py` and `scripts/tests/test_ta_lib/test_seed_yahoo.py`
- `TAService._ib_sleep()` and IB-specific 10-min inter-batch pacing
- `ib_client` parameter from `TAService.__init__`
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md scripts/CLAUDE.md CHANGELOG.md
git commit -m "docs: Data Source Priority → Massive for historical OHLCV"
```

---

## Task 11: Cutover runbook + FastAPI scheduler smoke test

**Files:**

- None modified — this is a weekend operator runbook. Document verification in PR description.

- [ ] **Step 1: Pre-flight — API key + smoke test**

```bash
cd /Users/chenxi/projects/xenon
TZ=America/New_York date +"%A %H:%M"          # confirm weekend / off-hours
python3.13 -c "from scripts.clients.massive_client import MassiveClient; \
  print(MassiveClient().health_check())"
# Expect: True
```

- [ ] **Step 2: Backup existing DuckDB**

```bash
cp data/ta.duckdb data/ta.duckdb.pre-massive.bak
ls -lh data/ta.duckdb*
```

- [ ] **Step 3: Re-seed 2 years, both timeframes**

```bash
python3.13 scripts/ta_reseed_massive.py --timeframes 1d,1h --lookback-days 730
```

Expected: runs ~minutes; summary "Done. 0 failures." (a few <5 acceptable for thin tickers).

- [ ] **Step 4: Verify stats**

```bash
python3.13 scripts/ta_cli.py --stats
```

Expected: ~500 1d bars/ticker, ~3500 1h bars/ticker (2 years × ~6.5h RTH).

- [ ] **Step 5: Spot-check against IB TWS (manual)**

```bash
python3.13 scripts/ta_cli.py AAPL TSLA SPY --history | tail -20
```

Visually compare last 5 closes against IB TWS or a trusted chart. Record results in PR description.

- [ ] **Step 6: Run E2E-3 (vendor divergence gate) — must pass**

```bash
XENON_ENABLE_IB_E2E=1 python3.13 -m pytest -m e2e -xvs \
  scripts/tests/test_e2e_massive_pipeline.py::test_e2e_3_vendor_divergence_ib_vs_massive
```

Expected: all three tickers pass with `<0.5%` close-price divergence on last 5 bars.

- [ ] **Step 7: Dry-run pre-market prep**

```bash
python3.13 scripts/ta_premarket_prep.py --audit-only
# Expect exit 0, no network activity even if MASSIVE_API_KEY unset
unset MASSIVE_API_KEY
python3.13 scripts/ta_premarket_prep.py --audit-only
# Expect exit 0 still
export MASSIVE_API_KEY=<your-key>
```

- [ ] **Step 8: Run trend-scan off new cache**

```bash
python3.13 scripts/trend_scan.py
```

Expected: output count within ±20% of prior baseline.

- [ ] **Step 9: FastAPI scheduler smoke (optional but recommended)**

Start the server. Tail logs. Verify the 6 AM ET pre-market loop handles exit code 3 without crashing:

```bash
# Simulate a failed run locally (one-off):
MASSIVE_API_KEY=invalid python3.13 scripts/ta_premarket_prep.py; echo "exit=$?"
# Expect exit=3 with aborted status file.
cat data/ta_premarket_status.json
```

Confirm `scripts/api/server.py` `run_script("ta_premarket_prep.py", ...)` logs a WARN but doesn't crash the loop. No code change required — verification only.

- [ ] **Step 10: Record in PR**

In the PR description, check off each runbook step and paste key output (stats, spot-check divergences, E2E-3 results). Note the cutover timestamp in PR body.

---

## Self-review

**Spec coverage:**

| Spec section                                                                         | Task                                                                             |
| ------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| §5.1 `MassiveClient`                                                                 | Task 1                                                                           |
| §5.1 `fetch_bars` rewrite + overlap + ET-aware date                                  | Task 3                                                                           |
| §5.1 `TAService` refactor + `BulkRefreshResult` + `_is_stale` 1h + no IB sleep       | Task 3                                                                           |
| §5.1 `ta_premarket_prep` lazy + status + exit 3 + pre-computed refresh set           | Task 4                                                                           |
| §5.1 `ta_cli` — construct client unless `--cache-only`                               | Task 5                                                                           |
| §5.1 `trend_scan.py:803` + status reader + `warnings` output field + stale-age check | Task 6                                                                           |
| §5.1 existing test files updated                                                     | Task 3 (bars/service/snapshot), Task 4 (premarket_prep), Task 5 (ta_cli_offline) |
| §5.1 `scripts/api/server.py` verify                                                  | Task 11 Step 9                                                                   |
| §5.2 MassiveClient API + exceptions + retries                                        | Task 1                                                                           |
| §5.3 data flow + timezone ET                                                         | Task 1, Task 3                                                                   |
| §5.4 1h freshness contract                                                           | Task 3                                                                           |
| §6 error handling, exit code 3, status file                                          | Tasks 1, 4, 6                                                                    |
| §7.2 implementation staged commits                                                   | Tasks 1–10 (one commit per task)                                                 |
| §7.3 cutover runbook                                                                 | Task 11                                                                          |
| §7.4 rollback                                                                        | `data/ta.duckdb.pre-massive.bak` created Task 11 Step 2                          |
| §8 unit / integration / E2E tests                                                    | Tasks 1, 3, 4, 7                                                                 |
| §8.5 `pyproject.toml` e2e marker                                                     | Task 7                                                                           |
| §9 policy docs                                                                       | Task 10                                                                          |
| §10 risks                                                                            | All mitigations covered; DST regression in Task 3                                |
| §11 follow-ups                                                                       | Not in this plan — deferred to Phase 2 per spec                                  |
| §12 success criteria                                                                 | Verified via Task 11 runbook                                                     |

**Placeholder scan:** No "TBD", "implement later", or "similar to Task N" without repeating code. Every code step has complete code. `scan_warnings` now has a concrete `build_scan_output()` contract in Task 6.

**Type consistency:**

- `MassiveClient.get_aggregates(ticker, timeframe, from_date, to_date, adjusted)` — same across Tasks 1, 3, 7, 8.
- `fetch_bars(massive_client, ticker, timeframe, lookback_days, overlap_days=5, end_date="")` — same across Tasks 3, 8.
- `TAService(db_path, massive_client=...)` — same across Tasks 3, 4, 5, 6, 7, 8.
- `BulkRefreshResult(attempted, succeeded, failed, failed_tickers, aborted)` — introduced Task 3, consumed in Task 4.
- `TaPremarketStatus` dataclass — same across Tasks 2, 4, 6.
- `STATUS_FILE = Path("data/ta_premarket_status.json")` — same across Tasks 4, 6.
- `EXIT_VENDOR_DOWN = 3` — Task 4 constant; Task 11 Step 9 verifies.
- `_build_massive_client()` — factory function in Tasks 4, 5 (monkeypatchable in tests).
- `build_scan_output(candidates, warnings, ...)` — introduced Task 6.

No inconsistencies found.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-15-massive-historical-data-migration.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using `executing-plans`, batch execution with checkpoints.

Which approach?
