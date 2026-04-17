# Apex R2 ETL — Tribunal Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Address the 10 findings from the 2026-04-17 Codex + Claude tribunal review of `feat/apex-r2-etl`. No new features — all work is production-hardening: narrow error handling, add rollback/recovery, eliminate dead UW dependencies, sanitize edge-case numerics, tighten cron.

**Architecture:** Surgical edits to existing modules. Each task is one to three files, all have tests, all commit independently so the work can be bisected or sliced into multiple PRs.

**Tech Stack:** Same as `feat/apex-r2-etl` — Python 3.13, pandas 2.x, pyarrow, boto3, pytest.

**Context:** The Apex R2 ETL shipped in `feat/apex-r2-etl` (commits `224b262..4ccdebe`). The final tribunal surfaced 10 findings: 6 consensus items (T1–T6, Codex + Claude) and 4 Claude-only MINOR items (C7–C10) verified against the codebase. This plan translates them into concrete tasks.

**Source findings:** See the chat transcript of the tribunal review; a condensed table appears below.

---

## Findings table (source of truth)

| ID  | Severity  | File:line                                   | Title                                                                 |
| --- | --------- | ------------------------------------------- | --------------------------------------------------------------------- |
| T1  | IMPORTANT | `scripts/apex_refresh.py:375`               | A18 broad `except` masks `MassiveAuthError`                           |
| T2  | IMPORTANT | `scripts/apex_refresh.py:~218-220`          | `refresh_one` two-object PUT is not atomic across R2                  |
| T3  | IMPORTANT | `scripts/ta_lib/apex_sync.py:172-176`       | Two-rename atomic swap has a kill-window                              |
| T4  | IMPORTANT | `scripts/trend_scan.py:302-303`             | `fetch_ohlcv` hard-depends on UW `stock_info` for unused `market_cap` |
| T5  | MINOR     | `scripts/apex_refresh.py:148`               | `inf` survives div-by-zero in indicator pipeline                      |
| T6  | MINOR     | `scripts/ta_lib/apex_sync.py:63-72`         | `downloaded += 1` race under `ThreadPoolExecutor`                     |
| C7  | MINOR     | `scripts/trend_scan.py:~588`                | `_filter_universe_to_covered` hardcodes `timeframes=("1d",)`          |
| C8  | MINOR     | `.github/workflows/apex-data-refresh.yml:9` | Saturday runs both incremental and full crons                         |
| C9  | MINOR     | `scripts/ta_lib/dry_run_store.py:59`        | `DryRunStore.list_objects` uses OS-native separators                  |
| C10 | MINOR     | spec on `trend-scan-cleanup`                | A20 RSI-threshold spec drift not yet applied                          |

**Execution order recommendation:** T1 → T4 → T5 → T6 → T2 → T3 → C7 → C8 → C9 → C10. T1/T4 are the cheapest high-value safety fixes (do before merge). T2/T3 are structural and best done during soak.

---

## File Structure

All edits live inside `feat/apex-r2-etl` (or a successor branch cut from it).

**Modified files:**

- `scripts/apex_refresh.py` — T1 (A18 narrow except), T2 (two-PUT rollback), T5 (inf sanitize)
- `scripts/ta_lib/apex_sync.py` — T3 (swap-window self-heal), T6 (thread-safe counter)
- `scripts/ta_lib/service.py` — T5 helper (optional: keep coercion boundary consistent)
- `scripts/ta_lib/dry_run_store.py` — C9 POSIX separators
- `scripts/trend_scan.py` — T4 (UW dependency softening), C7 (timeframe pass-through)
- `.github/workflows/apex-data-refresh.yml` — C8 (cron narrowing)

**Test-only changes (per task):**

- `scripts/tests/test_apex_refresh.py` — T1, T2, T5 regression tests
- `scripts/tests/test_apex_sync.py` — T3 self-heal test, T6 deterministic counter test
- `scripts/tests/test_trend_scan.py` — T4 fetcher-degrades test, C7 multi-tf test
- `scripts/tests/test_ta_lib/test_dry_run_store.py` (new if absent; otherwise add a test) — C9

**Cross-branch action:**

- `docs/superpowers/specs/2026-04-16-apex-r2-etl-design.md` on the `trend-scan-cleanup` anchor — C10 (RSI threshold)

**Branch:** Do this work on `feat/apex-r2-etl` (or a successor branch cut from its HEAD, e.g. `feat/apex-r2-etl-followups`). Do NOT branch off master — we need the Task 1–15 work as the baseline.

---

## Task 1: T1 — Narrow A18 `_incremental_session_ready` except clause

**Why this first:** Five-line change; eliminates a subtle silent-fail mode before production.

**Files:**

- Modify: `scripts/apex_refresh.py` (function `_incremental_session_ready`)
- Modify: `scripts/tests/test_apex_refresh.py` (append)

- [ ] **Step 1: Write the failing regression test**

Append to `scripts/tests/test_apex_refresh.py`:

```python
def test_a18_session_not_ready_on_massive_auth_error(monkeypatch):
    """T1: MassiveAuthError must NOT fall through to 'proceed anyway'."""
    from datetime import datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    from scripts.apex_refresh import _incremental_session_ready
    from scripts.clients.massive_client import MassiveAuthError

    r2 = MagicMock()
    now = datetime(2025, 11, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))

    def raise_auth(*a, **kw):
        raise MassiveAuthError("MASSIVE_API_KEY not set")
    monkeypatch.setattr("scripts.apex_refresh.MassiveClient", lambda: (_ for _ in ()).throw(MassiveAuthError("MASSIVE_API_KEY not set")))

    ready, reason = _incremental_session_ready(r2, now_et=now)
    assert not ready
    assert "MassiveAuthError" in reason or "MASSIVE_API_KEY" in reason


def test_a18_session_tolerates_transient_network_probe_failure(monkeypatch):
    """T1: genuinely transient probe errors (requests.RequestException) should still 'proceed'
    so a flaky DNS blip doesn't kill the nightly."""
    from datetime import datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    import requests
    from scripts.apex_refresh import _incremental_session_ready

    r2 = MagicMock()
    now = datetime(2025, 11, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))

    massive = MagicMock()
    monkeypatch.setattr("scripts.apex_refresh.MassiveClient", lambda: massive)

    def flake(*a, **kw):
        raise requests.ConnectionError("DNS blip")
    monkeypatch.setattr("scripts.apex_refresh.fetch_bars", flake)

    ready, reason = _incremental_session_ready(r2, now_et=now)
    assert ready, f"transient network probe error should still proceed; got reason={reason}"
```

- [ ] **Step 2: Run tests — expect `test_a18_session_not_ready_on_massive_auth_error` to FAIL**

```bash
python3.13 -m pytest scripts/tests/test_apex_refresh.py::test_a18_session_not_ready_on_massive_auth_error -xvs 2>&1 | tail -10
```

Expected: FAIL — current code returns `(True, "")` for `MassiveAuthError` via the broad except.

- [ ] **Step 3: Tighten `_incremental_session_ready`**

Replace the function body in `scripts/apex_refresh.py`. Find the current:

```python
def _incremental_session_ready(r2, *, now_et: datetime | None = None) -> tuple[bool, str]:
    """A18: return (ready, reason). If not ready, caller should exit 0 and defer."""
    from scripts.clients.massive_client import MassiveNoDataError

    now_et = now_et or datetime.now(_ET)
    if now_et.weekday() < 5 and now_et.hour < _MARKET_CLOSE_HOUR:
        return False, f"pre-close ({now_et:%Y-%m-%d %H:%M ET}) — defer"

    target = _prior_trading_day(now_et)
    try:
        massive = MassiveClient()
        fetch_bars(massive, "SPY", timeframe="1d", start=target, end=target)
    except MassiveNoDataError:
        return False, f"Massive has not published SPY 1d for {target} yet — defer"
    except Exception as exc:  # noqa: BLE001
        logger.warning("A18 probe failed with %s; proceeding anyway", exc)
    return True, ""
```

Replace with:

```python
def _incremental_session_ready(r2, *, now_et: datetime | None = None) -> tuple[bool, str]:
    """A18: return (ready, reason). If not ready, caller should exit 0 and defer.

    Error handling (T1):
      MassiveNoDataError      -> defer cleanly (vendor caught up later)
      MassiveAuthError        -> defer with loud reason (config issue; no point running)
      requests.RequestException / MassiveRateLimitError -> proceed (transient; main
                                 run will retry per-ticker and surface real failures)
      any other exception     -> defer with reason (fail-closed; unknown state)
    """
    import requests

    from scripts.clients.massive_client import (
        MassiveAuthError,
        MassiveNoDataError,
        MassiveRateLimitError,
    )

    now_et = now_et or datetime.now(_ET)
    if now_et.weekday() < 5 and now_et.hour < _MARKET_CLOSE_HOUR:
        return False, f"pre-close ({now_et:%Y-%m-%d %H:%M ET}) — defer"

    target = _prior_trading_day(now_et)
    try:
        massive = MassiveClient()
        fetch_bars(massive, "SPY", timeframe="1d", start=target, end=target)
    except MassiveNoDataError:
        return False, f"Massive has not published SPY 1d for {target} yet — defer"
    except MassiveAuthError as exc:
        return False, f"MassiveAuthError during A18 probe: {exc} — defer"
    except (MassiveRateLimitError, requests.RequestException) as exc:
        logger.warning("A18 probe transient error: %s — proceeding (run will retry)", exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"A18 probe failed unexpectedly ({type(exc).__name__}: {exc}) — defer"
    return True, ""
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
python3.13 -m pytest scripts/tests/test_apex_refresh.py -q 2>&1 | tail -5
```

Expected: all tests pass (new 2 + existing 32 = 34).

- [ ] **Step 5: Commit**

```bash
git add scripts/apex_refresh.py scripts/tests/test_apex_refresh.py
git commit -m "fix(apex_refresh): T1 narrow A18 probe except — defer on auth, proceed on transient"
```

---

## Task 2: T4 — Soften UW `stock_info` hard dependency in `LiveTrendDataFetcher.fetch_ohlcv`

**Why:** `_stock_info(ticker)` call propagates UW outages as ticker drops, AND the field it populates (`market_cap`) is no longer consumed after Task 11 moved Stage A to read `marketCap` from `universe_row`.

**Files:**

- Modify: `scripts/trend_scan.py` (lines 264–305, `fetch_ohlcv`)
- Modify: `scripts/tests/test_trend_scan.py` (append)

- [ ] **Step 1: Verify `snapshot["market_cap"]` is truly dead weight**

```bash
grep -R --include="*.py" -n 'snapshot\["market_cap"\]\|snapshot\.get("market_cap"\|ohlcv\["market_cap"\]\|ohlcv\.get("market_cap"\|indicators\["market_cap"\]' scripts/ web/ 2>&1 | head -10
```

Expected: the ONLY write site is `scripts/trend_scan.py:303`. No read site exists downstream of `fetch_ohlcv`. If this grep surfaces any consumer, STOP and report; else proceed.

- [ ] **Step 2: Write the failing test**

Append to `scripts/tests/test_trend_scan.py`:

```python
def test_fetch_ohlcv_does_not_drop_ticker_on_uw_stock_info_failure():
    """T4: a transient UW stock_info failure must NOT turn a valid OHLCV into None."""
    from unittest.mock import MagicMock

    from scripts.trend_scan import LiveTrendDataFetcher

    ta_service = MagicMock()
    ta_service.get_snapshot.return_value = {
        "ticker": "AAPL",
        "close": 200.0,
        "price": 200.0,
        "dollar_volume": 4e10,
        # ... scanner contract keys
        "ma_20": 195.0, "ma_50": 190.0, "ma_200": 170.0,
        "rsi": 55.0, "adx": 20.0, "bbw": 0.04,
        "macd": 0.5, "macd_signal": 0.3, "macd_histogram": 0.2,
        "ma_20_series": [193.0, 194.0, 194.5, 195.0, 195.0],
        "recent_avg_volume": 5e7, "avg_20d_volume": 4e7, "recent_up_ratio": 0.6,
        "high_52w": 290.0, "range_20d_pct": 0.08, "atr_pct": 0.02,
        "volume": 5e7, "open": 199.0, "high": 201.0, "low": 198.0,
    }

    uw = MagicMock()
    uw.get_stock_info.side_effect = RuntimeError("UW outage")

    fetcher = LiveTrendDataFetcher(uw_client=uw, ta_service=ta_service)
    result = fetcher.fetch_ohlcv("AAPL")

    assert result is not None, "UW stock_info outage must not drop the ticker"
    assert result["close"] == 200.0
    # market_cap falls back to 0.0 (or is absent) but everything else survives
    assert result.get("market_cap", 0.0) == 0.0
```

- [ ] **Step 3: Run — expect FAIL**

```bash
python3.13 -m pytest scripts/tests/test_trend_scan.py::test_fetch_ohlcv_does_not_drop_ticker_on_uw_stock_info_failure -xvs 2>&1 | tail -10
```

Expected: FAIL. The current `fetch_ohlcv` calls `self._stock_info(ticker)` which raises.

- [ ] **Step 4: Modify `fetch_ohlcv` to soft-fail on UW**

In `scripts/trend_scan.py`, locate the `fetch_ohlcv` method (around line 264). Replace the trailing `info = self._stock_info(ticker)` / `snapshot["market_cap"] = ...` block with a best-effort variant. Specifically, change:

```python
        snapshot["rs_vs_spy"] = rs_vs_spy

        info = self._stock_info(ticker)
        snapshot["market_cap"] = _safe_float(info.get("marketcap") or info.get("market_cap") or info.get("marketCap"))

        return snapshot
```

to:

```python
        snapshot["rs_vs_spy"] = rs_vs_spy

        # T4: UW stock_info is best-effort. Stage A reads marketCap from universe_row
        # (meta/universe.json), so a UW outage must not drop the ticker here.
        try:
            info = self._stock_info(ticker)
            snapshot["market_cap"] = _safe_float(
                info.get("marketcap") or info.get("market_cap") or info.get("marketCap")
            )
        except Exception:
            logger.debug("UW stock_info unavailable for %s — skipping market_cap", ticker, exc_info=True)
            snapshot["market_cap"] = 0.0

        return snapshot
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
python3.13 -m pytest scripts/tests/test_trend_scan.py -q 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/trend_scan.py scripts/tests/test_trend_scan.py
git commit -m "fix(trend_scan): T4 soft-fail on UW stock_info; don't drop ticker on outage"
```

---

## Task 3: T5 — Sanitize `inf` from indicator division-by-zero

**Files:**

- Modify: `scripts/apex_refresh.py` (function `_compute_indicators_adapter`)
- Modify: `scripts/tests/test_apex_refresh.py` (append)

- [ ] **Step 1: Write failing test**

```python
def test_compute_indicators_adapter_handles_zero_close_without_inf():
    """T5: a zero close row must not emit inf for atr_pct / range_20d_pct."""
    import numpy as np
    import pandas as pd

    from scripts.apex_refresh import _compute_indicators_adapter

    n = 60
    # Day 30 has close=0 (pathological but defensible to guard)
    closes = [100.0 + i * 0.1 for i in range(n)]
    closes[29] = 0.0
    ohlcv = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC"),
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    })
    ind = _compute_indicators_adapter(ohlcv)

    finite_mask = np.isfinite(ind["atr_pct"])
    # the zero-close row itself may be NaN but MUST NOT be inf
    assert not np.isinf(ind["atr_pct"]).any(), "atr_pct must never be ±inf"
    assert not np.isinf(ind["range_20d_pct"]).any(), "range_20d_pct must never be ±inf"
```

- [ ] **Step 2: Run — expect FAIL**

```bash
python3.13 -m pytest scripts/tests/test_apex_refresh.py::test_compute_indicators_adapter_handles_zero_close_without_inf -xvs 2>&1 | tail -10
```

Expected: FAIL (current code produces `inf` at the zero-close row).

- [ ] **Step 3: Guard the divisions**

In `scripts/apex_refresh.py`, locate `_compute_indicators_adapter`. Change:

```python
    enriched["range_20d_pct"] = (
        enriched["high"].rolling(20, min_periods=20).max() - enriched["low"].rolling(20, min_periods=20).min()
    ) / enriched["close"]
    enriched["atr_pct"] = enriched["atr_14"] / enriched["close"]
```

to:

```python
    import numpy as np

    # T5: guard div-by-zero so inf never reaches the indicator parquet.
    # close may be 0.0 (pathological) — let the result be NaN and rely on
    # TAService._coerce_float to map NaN -> 0.0 at the scanner boundary.
    safe_close = enriched["close"].where(enriched["close"] > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        range_numerator = (
            enriched["high"].rolling(20, min_periods=20).max()
            - enriched["low"].rolling(20, min_periods=20).min()
        )
        enriched["range_20d_pct"] = (range_numerator / safe_close).replace(
            [np.inf, -np.inf], np.nan
        )
        enriched["atr_pct"] = (enriched["atr_14"] / safe_close).replace(
            [np.inf, -np.inf], np.nan
        )
```

(If `numpy as np` is already imported at module level, drop the `import numpy as np` inside the function.)

- [ ] **Step 4: Run tests — expect PASS**

```bash
python3.13 -m pytest scripts/tests/test_apex_refresh.py -q 2>&1 | tail -3
```

- [ ] **Step 5: Commit**

```bash
git add scripts/apex_refresh.py scripts/tests/test_apex_refresh.py
git commit -m "fix(apex_refresh): T5 sanitize inf from atr_pct / range_20d_pct div-by-zero"
```

---

## Task 4: T6 — Thread-safe counter in `apex_sync._download_prefix`

**Files:**

- Modify: `scripts/ta_lib/apex_sync.py` (function `_download_prefix`)
- Modify: `scripts/tests/test_apex_sync.py` (append)

- [ ] **Step 1: Write failing determinism test**

Append to `scripts/tests/test_apex_sync.py`:

```python
def test_download_prefix_counter_deterministic_under_concurrency(tmp_path):
    """T6: with many keys and 10 workers, the returned `downloaded` count must equal
    the number of successful downloads, never miscount via the nonlocal += race."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from scripts.ta_lib.apex_sync import _download_prefix

    n_keys = 200
    keys = [(f"parquet/historical/1d/T{i:03d}.parquet", 100, None) for i in range(n_keys)]

    r2 = MagicMock()
    r2.list_objects.return_value = iter(keys)
    r2.get_object.return_value = b"x" * 100

    for _ in range(5):  # run a few times to surface timing races
        # fresh target dir each iteration
        target = tmp_path / "sync"
        if target.exists():
            import shutil
            shutil.rmtree(target)
        target.mkdir()
        downloaded, errors = _download_prefix(r2, "parquet/historical/1d/", target, max_workers=10)
        assert errors == []
        assert downloaded == n_keys, f"expected {n_keys}, got {downloaded}"
```

- [ ] **Step 2: Run — may or may not FAIL depending on timing**

```bash
python3.13 -m pytest scripts/tests/test_apex_sync.py::test_download_prefix_counter_deterministic_under_concurrency -xvs 2>&1 | tail -5
```

With CPython's GIL this race is hard to reproduce in practice, so the test may pass on the current code. That's fine — the refactor still fixes the formal issue.

- [ ] **Step 3: Refactor `_download_prefix` to return counts from workers**

In `scripts/ta_lib/apex_sync.py`, replace the function:

```python
def _download_prefix(r2, prefix: str, target_root: Path, max_workers: int) -> tuple[int, list[str]]:
    keys = [k for k, _size, _mtime in r2.list_objects(prefix)]
    errors: list[str] = []

    def _one(key: str) -> int:
        try:
            body = r2.get_object(key)
            target = target_root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            return 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key}: {exc}")
            return 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        downloaded = sum(ex.map(_one, keys))

    return downloaded, errors
```

Note: `errors.append` on `list` is thread-safe in CPython (bytecode-level append via the GIL), so no lock is needed there. Only the counter changed.

- [ ] **Step 4: Run all apex_sync tests — PASS**

```bash
python3.13 -m pytest scripts/tests/test_apex_sync.py -q 2>&1 | tail -3
```

- [ ] **Step 5: Commit**

```bash
git add scripts/ta_lib/apex_sync.py scripts/tests/test_apex_sync.py
git commit -m "fix(apex_sync): T6 thread-safe download count via ex.map return values"
```

---

## Task 5: T2 — `refresh_one` rollback on indicator PUT failure

**Why:** per-ticker atomicity today: both buffers serialized before PUT, but the two PUTs are sequential. If historical succeeds and indicator fails, the ticker is mismatched until the next full refresh. Fix: on indicator PUT failure, delete the historical PUT (restoring whatever was there before, or leaving absent).

**Files:**

- Modify: `scripts/apex_refresh.py` (function `refresh_one`)
- Modify: `scripts/ta_lib/r2_store.py` (add `delete_object`)
- Modify: `scripts/ta_lib/dry_run_store.py` (matching `delete_object` stub)
- Modify: `scripts/tests/test_r2_store.py` (+ delete test)
- Modify: `scripts/tests/test_apex_refresh.py` (+ rollback test)

- [ ] **Step 1: Add `delete_object` to `R2Store`**

Failing test first. Append to `scripts/tests/test_r2_store.py`:

```python
@mock_aws
def test_delete_object_roundtrip(r2_env):
    import boto3
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="apex-data")

    from scripts.ta_lib.r2_store import R2Store, R2NotFoundError

    r2 = R2Store()
    r2.put_object("meta/test.bin", b"xyz")
    r2.delete_object("meta/test.bin")
    import pytest
    with pytest.raises(R2NotFoundError):
        r2.get_object("meta/test.bin")


@mock_aws
def test_delete_object_missing_is_noop(r2_env):
    """Deleting a missing key must not raise — the rollback path relies on idempotency."""
    import boto3
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="apex-data")

    from scripts.ta_lib.r2_store import R2Store

    R2Store().delete_object("does/not/exist")  # no raise
```

Then in `scripts/ta_lib/r2_store.py`, add method to `R2Store`:

```python
    def delete_object(self, key: str) -> None:
        """Idempotent delete — raises R2ClientError only on non-404 failure."""
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                return
            raise R2ClientError(f"delete_object({key!r}): {code}") from exc
```

Add matching method to `DryRunStore` in `scripts/ta_lib/dry_run_store.py`:

```python
    def delete_object(self, key: str) -> None:
        """Idempotent delete for dry-run / tests."""
        path = self._path(key)
        if path.exists():
            path.unlink()
```

Run: `python3.13 -m pytest scripts/tests/test_r2_store.py -q 2>&1 | tail -3` → PASS.

- [ ] **Step 2: Write failing rollback test**

Append to `scripts/tests/test_apex_refresh.py`:

```python
def test_refresh_one_rolls_back_historical_put_when_indicator_put_fails():
    """T2: if indicator PUT fails after historical PUT succeeded, delete the historical
    object so the ticker's two-parquet state stays consistent (both present or neither).
    """
    import pandas as pd
    from unittest.mock import MagicMock

    from scripts.apex_refresh import refresh_one

    n = 300
    massive = MagicMock()
    massive.get_aggregates.return_value = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=n, freq="D", tz="America/New_York"),
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
        "close": [100.5] * n, "volume": [1_000_000] * n,
        "vwap": [100.25] * n, "tx_count": [150] * n,
    })

    r2 = MagicMock()
    written: dict[str, bytes] = {}

    hist_key = "parquet/historical/1d/AAPL.parquet"
    ind_key = "parquet/indicators/1d/AAPL.parquet"

    def fake_put(key, body, if_match=None):
        if key == ind_key:
            raise RuntimeError("simulated indicator PUT failure")
        written[key] = body
        return '"etag"'

    deleted: list[str] = []
    def fake_delete(key):
        deleted.append(key)
        written.pop(key, None)

    r2.put_object.side_effect = fake_put
    r2.delete_object.side_effect = fake_delete

    result = refresh_one(r2=r2, massive=massive, ticker="AAPL", timeframe="1d", mode="full")

    assert not result.succeeded
    # Historical was PUT then rolled back
    assert hist_key in deleted, "expected historical rollback; deletes=%r" % deleted
    assert hist_key not in written, "historical object must be gone after rollback"
```

- [ ] **Step 3: Run — FAIL (no rollback yet)**

```bash
python3.13 -m pytest scripts/tests/test_apex_refresh.py::test_refresh_one_rolls_back_historical_put_when_indicator_put_fails -xvs 2>&1 | tail -10
```

- [ ] **Step 4: Implement rollback in `refresh_one`**

In `scripts/apex_refresh.py`, locate the two PUTs at the bottom of the try block (currently around lines 218-219):

```python
        # Only now issue PUTs
        r2.put_object(hist_key, hist_buf.getvalue())
        r2.put_object(ind_key, ind_buf.getvalue())

        return RefreshResult(ticker, timeframe, succeeded=True, rows_written=len(ohlcv))
```

Replace with:

```python
        # T2: historical first, then indicators. If indicators fails, roll back
        # the historical PUT so the ticker's two-parquet state stays consistent.
        r2.put_object(hist_key, hist_buf.getvalue())
        try:
            r2.put_object(ind_key, ind_buf.getvalue())
        except Exception as ind_exc:
            try:
                r2.delete_object(hist_key)
                logger.warning(
                    "rolled back historical PUT for %s/%s after indicator failure: %s",
                    ticker, timeframe, ind_exc,
                )
            except Exception:
                logger.exception(
                    "FAILED to roll back historical PUT for %s/%s — manual cleanup may be needed",
                    ticker, timeframe,
                )
            raise

        return RefreshResult(ticker, timeframe, succeeded=True, rows_written=len(ohlcv))
```

The `raise` falls through to the outer `except Exception` in `refresh_one` which already converts to `RefreshResult(succeeded=False, error=...)`.

- [ ] **Step 5: Run tests — PASS**

```bash
python3.13 -m pytest scripts/tests/test_apex_refresh.py -q 2>&1 | tail -3
```

- [ ] **Step 6: Commit**

```bash
git add scripts/apex_refresh.py scripts/ta_lib/r2_store.py scripts/ta_lib/dry_run_store.py \
        scripts/tests/test_r2_store.py scripts/tests/test_apex_refresh.py
git commit -m "fix(apex_refresh): T2 rollback historical PUT when indicator PUT fails"
```

---

## Task 6: T3 — `apex_sync` swap-window self-heal

**Why:** the two-rename swap has a kill-window where `mirror_dir` is gone but `.tmp` (or `.old`) still holds the payload. Add a startup self-heal that detects and recovers this state.

**Files:**

- Modify: `scripts/ta_lib/apex_sync.py` (add `_recover_from_interrupted_swap`, call at top of `sync_if_stale`)
- Modify: `scripts/tests/test_apex_sync.py` (+ recovery test)

- [ ] **Step 1: Write failing recovery test**

Append to `scripts/tests/test_apex_sync.py`:

```python
def test_sync_recovers_from_interrupted_swap_where_only_old_exists(tmp_path, monkeypatch):
    """T3: mirror_dir is missing and <mirror_dir>.old exists — recover by renaming .old back
    BEFORE any R2 call."""
    import json
    from pathlib import Path
    from unittest.mock import MagicMock

    from scripts.ta_lib.apex_sync import sync_if_stale

    # Simulate interrupted swap: only .old exists, live mirror is gone
    old_dir = tmp_path.with_name(tmp_path.name + ".old")
    (old_dir / "meta").mkdir(parents=True, exist_ok=True)
    (old_dir / "meta" / "universe.json").write_text('{"tickers": []}')
    (old_dir / ".last_sync.json").write_text(
        json.dumps({"historical": "2026-04-15T00:00:00+00:00", "indicators": "2026-04-15T00:00:00+00:00", "schema_version": 1})
    )
    if tmp_path.exists():
        import shutil
        shutil.rmtree(tmp_path)

    # R2 reports same ts as the recovered mirror's .last_sync → not stale → no download
    r2 = MagicMock()
    r2.get_json.return_value = {
        "historical": "2026-04-15T00:00:00+00:00",
        "indicators": "2026-04-15T00:00:00+00:00",
        "schema_version": 1,
    }

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2)
    assert result.synced is False
    # Live mirror is back
    assert (tmp_path / "meta" / "universe.json").exists()
    # .old is gone
    assert not old_dir.exists()


def test_sync_recovers_from_interrupted_swap_where_only_tmp_exists(tmp_path):
    """T3: mirror_dir is missing and <mirror_dir>.tmp exists — accept .tmp as the new
    mirror (it's been fully written including .last_sync.json — Task 8 only renames
    tmp once .last_sync.json is inside it)."""
    import json
    from unittest.mock import MagicMock

    from scripts.ta_lib.apex_sync import sync_if_stale

    tmp_dir = tmp_path.with_name(tmp_path.name + ".tmp")
    (tmp_dir / "meta").mkdir(parents=True, exist_ok=True)
    (tmp_dir / "meta" / "universe.json").write_text('{"tickers": []}')
    (tmp_dir / ".last_sync.json").write_text(
        json.dumps({"historical": "2026-04-16T00:00:00+00:00", "indicators": "2026-04-16T00:00:00+00:00", "schema_version": 1})
    )
    if tmp_path.exists():
        import shutil; shutil.rmtree(tmp_path)

    r2 = MagicMock()
    r2.get_json.return_value = {
        "historical": "2026-04-16T00:00:00+00:00",
        "indicators": "2026-04-16T00:00:00+00:00",
        "schema_version": 1,
    }

    result = sync_if_stale(mirror_dir=tmp_path, r2=r2)
    assert result.synced is False  # matches the mirror now, so no new sync
    assert (tmp_path / "meta" / "universe.json").exists()
    assert not tmp_dir.exists()
```

- [ ] **Step 2: Run — expect FAIL**

```bash
python3.13 -m pytest scripts/tests/test_apex_sync.py -xvs -k interrupted_swap 2>&1 | tail -10
```

- [ ] **Step 3: Add `_recover_from_interrupted_swap` and call it at the top of `sync_if_stale`**

In `scripts/ta_lib/apex_sync.py`, add before `sync_if_stale`:

```python
def _recover_from_interrupted_swap(mirror_dir: Path) -> None:
    """T3: before any sync work, heal an interrupted two-rename swap.

    Two failure shapes we repair:
      (a) mirror_dir missing + <mirror_dir>.old present  -> rename .old back
      (b) mirror_dir missing + <mirror_dir>.tmp present
          + .tmp has a .last_sync.json                   -> rename .tmp into place
    If mirror_dir is present, leave any stale .old/.tmp alone (the previous swap
    completed; .old/.tmp cleanup is a best-effort that may have been killed post-swap).
    """
    if mirror_dir.exists():
        return
    old_dir = mirror_dir.with_name(mirror_dir.name + ".old")
    tmp_dir = mirror_dir.with_name(mirror_dir.name + ".tmp")

    if tmp_dir.exists() and (tmp_dir / _LAST_SYNC_FILE).exists():
        logger.warning(
            "apex_sync: live mirror missing; promoting %s (interrupted swap recovery)",
            tmp_dir,
        )
        tmp_dir.rename(mirror_dir)
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)
        return

    if old_dir.exists():
        logger.warning(
            "apex_sync: live mirror missing; restoring %s (interrupted swap recovery)",
            old_dir,
        )
        old_dir.rename(mirror_dir)
```

At the TOP of `sync_if_stale` (right after `mirror_dir = Path(mirror_dir)`), insert:

```python
    _recover_from_interrupted_swap(mirror_dir)
```

- [ ] **Step 4: Run tests — PASS**

```bash
python3.13 -m pytest scripts/tests/test_apex_sync.py -q 2>&1 | tail -3
```

- [ ] **Step 5: Commit**

```bash
git add scripts/ta_lib/apex_sync.py scripts/tests/test_apex_sync.py
git commit -m "fix(apex_sync): T3 self-heal interrupted two-rename swap at startup"
```

---

## Task 7: C7 — Parameterize `_filter_universe_to_covered` timeframes

**Why:** today it's hardcoded `("1d",)`. Future-proof it.

**Files:**

- Modify: `scripts/trend_scan.py` (pass `timeframes` explicitly at call site)
- Modify: `scripts/tests/test_trend_scan.py` (+ multi-tf test)

- [ ] **Step 1: Write failing test**

```python
def test_filter_universe_to_covered_requires_all_requested_timeframes(tmp_path):
    """C7: if we request ('1d', '1h'), a ticker with only 1d parquet must be filtered out."""
    from scripts.trend_scan import _filter_universe_to_covered

    (tmp_path / "parquet" / "historical" / "1d").mkdir(parents=True)
    (tmp_path / "parquet" / "historical" / "1h").mkdir(parents=True)
    (tmp_path / "parquet" / "historical" / "1d" / "AAPL.parquet").write_bytes(b"x")
    (tmp_path / "parquet" / "historical" / "1h" / "AAPL.parquet").write_bytes(b"x")
    (tmp_path / "parquet" / "historical" / "1d" / "DAILY_ONLY.parquet").write_bytes(b"x")

    universe = [{"symbol": "AAPL"}, {"symbol": "DAILY_ONLY"}]
    covered, missing = _filter_universe_to_covered(tmp_path, universe, timeframes=("1d", "1h"))
    assert [r["symbol"] for r in covered] == ["AAPL"]
    assert missing == ["DAILY_ONLY"]
```

- [ ] **Step 2: Run — expect PASS already (the function accepts `timeframes` via kwarg; current test proves behavior).**

If it passes without changes, good. Then harden the call site:

- [ ] **Step 3: Define a module-level `_SCANNER_TIMEFRAMES` constant and use it everywhere**

In `scripts/trend_scan.py`, near other constants (right after the imports block, around line 45):

```python
# C7: single source of truth for the timeframes Stage A reads. Update here AND in
# apex_sync.sync_if_stale if/when the scanner consumes additional timeframes.
_SCANNER_TIMEFRAMES: tuple[str, ...] = ("1d",)
```

Find the `_filter_universe_to_covered` call site in `run_scan_pipeline`:

```python
covered_rows, missing_symbols = _filter_universe_to_covered(mirror_dir, universe_rows, timeframes=("1d",))
```

Replace with:

```python
covered_rows, missing_symbols = _filter_universe_to_covered(
    mirror_dir, universe_rows, timeframes=_SCANNER_TIMEFRAMES
)
```

Also pass it to `sync_if_stale`:

```python
sync_result = sync_if_stale(mirror_dir=mirror_dir, timeframes=_SCANNER_TIMEFRAMES)
```

- [ ] **Step 4: Run — PASS**

```bash
python3.13 -m pytest scripts/tests/test_trend_scan.py scripts/tests/test_apex_sync.py -q 2>&1 | tail -3
```

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan.py scripts/tests/test_trend_scan.py
git commit -m "refactor(trend_scan): C7 single source of truth for scanner timeframes"
```

---

## Task 8: C8 — Restrict incremental cron to weekdays

**Files:**

- Modify: `.github/workflows/apex-data-refresh.yml`

- [ ] **Step 1: Edit the cron**

In `.github/workflows/apex-data-refresh.yml`, find:

```yaml
on:
  schedule:
    # Mon-Fri after US close: Tue-Sat 01:00 UTC = Mon-Fri ~8-9 PM ET (DST-dependent)
    - cron: "0 1 * * 2-6"
    # Saturday full refresh: Sat 05:00 UTC = Fri night / early Sat ET
    - cron: "0 5 * * 6"
```

Change to:

```yaml
on:
  schedule:
    # C8: Mon-Fri after US close — incremental runs Tue-Fri 01:00 UTC. Saturday is
    # handled entirely by the full cron below; no need to double-run.
    - cron: "0 1 * * 2-5"
    # Saturday full refresh: Sat 05:00 UTC ≈ Fri ~midnight ET / early Sat ET
    - cron: "0 5 * * 6"
```

Note: the `mode: determination` block in the workflow keyed on `github.event.schedule == "0 5 * * 6"` still works — the incremental cron signature changes but the full cron's match is unchanged.

- [ ] **Step 2: Lint**

```bash
actionlint .github/workflows/apex-data-refresh.yml 2>/dev/null || echo "actionlint not installed; skipping"
```

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/apex-data-refresh.yml
git commit -m "ci: C8 drop Saturday incremental cron — full handles it"
```

---

## Task 9: C9 — `DryRunStore.list_objects` POSIX paths

**Files:**

- Modify: `scripts/ta_lib/dry_run_store.py`
- Create: `scripts/tests/test_ta_lib/test_dry_run_store.py` (if absent)

- [ ] **Step 1: Write failing test**

Create `scripts/tests/test_ta_lib/test_dry_run_store.py` (or append if it exists):

```python
"""Tests for scripts.ta_lib.dry_run_store."""
from __future__ import annotations


def test_list_objects_emits_posix_paths(tmp_path):
    """C9: keys returned from list_objects must use forward slashes regardless of OS."""
    from scripts.ta_lib.dry_run_store import DryRunStore

    r2 = DryRunStore(tmp_path / "preview")
    r2.put_object("parquet/historical/1d/AAPL.parquet", b"x")

    keys = [k for k, _size, _mtime in r2.list_objects("parquet/")]
    assert "parquet/historical/1d/AAPL.parquet" in keys
    for k in keys:
        assert "\\" not in k, f"key contains OS-native separator: {k!r}"
```

- [ ] **Step 2: Run — PASS on macOS/Linux already, but on Windows would fail. This locks the contract.**

```bash
python3.13 -m pytest scripts/tests/test_ta_lib/test_dry_run_store.py -xvs 2>&1 | tail -5
```

- [ ] **Step 3: Harden `list_objects`**

In `scripts/ta_lib/dry_run_store.py`, change:

```python
yield (
    str(p.relative_to(base)),
    p.stat().st_size,
    datetime.fromtimestamp(p.stat().st_mtime),
)
```

to:

```python
yield (
    p.relative_to(base).as_posix(),
    p.stat().st_size,
    datetime.fromtimestamp(p.stat().st_mtime),
)
```

- [ ] **Step 4: Run — PASS**

```bash
python3.13 -m pytest scripts/tests/test_ta_lib/test_dry_run_store.py -q 2>&1 | tail -3
```

- [ ] **Step 5: Commit**

```bash
git add scripts/ta_lib/dry_run_store.py scripts/tests/test_ta_lib/test_dry_run_store.py
git commit -m "fix(dry_run_store): C9 emit POSIX paths from list_objects"
```

---

## Task 10: C10 — Apply A20 to the spec on `trend-scan-cleanup` anchor

**Why:** the spec lives on `trend-scan-cleanup`, not on `feat/apex-r2-etl`. Before retiring the anchor branch, update §6 to match the shipped implementation.

**Files:**

- Modify: `docs/superpowers/specs/2026-04-16-apex-r2-etl-design.md` (on `trend-scan-cleanup` branch only)

- [ ] **Step 1: Switch to the anchor branch**

```bash
git checkout trend-scan-cleanup
```

Verify the spec exists:

```bash
ls docs/superpowers/specs/2026-04-16-apex-r2-etl-design.md
```

- [ ] **Step 2: Apply the edit**

In `docs/superpowers/specs/2026-04-16-apex-r2-etl-design.md`, locate §6 (around line 263 per the tribunal finding). Find:

```
Existing `passes_bullish_gate` / `passes_bearish_gate` in `ta_prefilter.py` still run after this — they handle trend direction (close vs ma_20, rsi > 50), not liquidity/size.
```

Replace with:

```
Existing `passes_bullish_gate` / `passes_bearish_gate` in `ta_prefilter.py` still run after this — they handle trend direction (bullish: `close > ma_20` AND `rsi > 40`; bearish mirror: `close < ma_20` AND `rsi < 60`), not liquidity/size. The earlier spec draft said `rsi > 50` — the implementation intentionally widened this to `> 40 / < 60` to preserve Stage A candidate yield; this comment is the single source of truth.
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-04-16-apex-r2-etl-design.md
git commit -m "docs(spec): A20 reconcile RSI threshold with shipped implementation (40/60)"
```

- [ ] **Step 4: Return to the feature branch**

```bash
git checkout feat/apex-r2-etl
```

(Or the successor branch you're working on.)

---

## Final verification

After all tasks, run the affected test suite and a quick live E2E:

```bash
python3.13 -m pytest \
  scripts/tests/test_r2_store.py \
  scripts/tests/test_parquet_store.py \
  scripts/tests/test_apex_sync.py \
  scripts/tests/test_apex_refresh.py \
  scripts/tests/test_ta_lib \
  scripts/tests/test_trend_scan.py \
  scripts/tests/test_trend_scan_lib -q
```

Expected: all green (target ≥ 120 tests after new regression cases added).

Then a 3-ticker live dry-run to confirm end-to-end is still healthy:

```bash
set -a && source .env && set +a
python3.13 -c "
from scripts.ta_lib.dry_run_store import DryRunStore
from scripts.apex_refresh import refresh_one
from scripts.clients.massive_client import MassiveClient
from pathlib import Path
import shutil

preview = Path('data/apex_mirror_preview')
if preview.exists(): shutil.rmtree(preview)
r2 = DryRunStore(preview)
massive = MassiveClient()
for t in ('AAPL', 'MSFT', 'SPY'):
    r = refresh_one(r2=r2, massive=massive, ticker=t, timeframe='1d', mode='full')
    print(f'{t}: succeeded={r.succeeded} rows={r.rows_written}')
"
```

Expected: 3 × `succeeded=True rows=~501`.

---

## Self-Review Checklist

**1. Spec coverage:** every finding in the table at the top maps to exactly one task — T1→Task 1, T4→Task 2, T5→Task 3, T6→Task 4, T2→Task 5, T3→Task 6, C7→Task 7, C8→Task 8, C9→Task 9, C10→Task 10. ✅

**2. Placeholder scan:** no TBD, no "handle appropriately," no "implement later," no "similar to Task N" — every step has its code or its command. ✅

**3. Type consistency:**

- `_recover_from_interrupted_swap` (Task 6) references `_LAST_SYNC_FILE` which is a module-level constant in `apex_sync.py` (from Task 8 of the original plan). ✅
- Task 5's `delete_object` method signature `(self, key: str) -> None` matches on both `R2Store` and `DryRunStore`. ✅
- `_SCANNER_TIMEFRAMES` (Task 7) is a tuple `(str, ...)`; matches what `_filter_universe_to_covered` and `sync_if_stale` expect. ✅
- The `fetch_bars(massive, "SPY", timeframe="1d", start=target, end=target)` call in the new A18 code (Task 1) matches the signature defined in Task 4 of the original plan. ✅

---

## Execution

This plan is ready. Recommended execution: superpowers:subagent-driven-development — each task is 5–10 minutes with a clear pre/post verification, so subagent dispatch + brief review is the right granularity. Task 10 requires switching branches so cannot run inside a worktree without care; handle it last or skip in a worktree-isolated session.
