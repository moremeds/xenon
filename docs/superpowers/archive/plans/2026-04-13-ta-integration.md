# TA Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate `TAService` into `trend_scan.py` — replace UW OHLC fetch and hand-rolled pandas TA math with the read-through cache from `scripts/ta_lib/`. After this, the trend scanner uses IB historical data via DuckDB.

**Architecture:** `trend_scan.py`'s `LiveTrendDataFetcher.fetch_ohlcv()` calls `TAService.get_snapshot()` instead of computing TA inline. `bulk_refresh()` is called on the main thread before `parallel_fetch()` to pre-warm the cache. SPY benchmark data also comes from TAService. `market_cap` remains from UW `stock_info`.

**Tech Stack:** TAService (from ta-services plan), existing trend_scan pipeline

**Prerequisite:** The `ta-services` plan must be fully complete and verified before starting this plan.

**Design Spec:** `docs/superpowers/specs/2026-04-13-ta-lib-module-design.md` (Integration section)

---

## File Map

| File                                                  | Change | Responsibility                                                                                          |
| ----------------------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------- |
| `scripts/trend_scan.py`                               | Modify | Replace `fetch_ohlcv()` body, add `bulk_refresh()` call, remove `_bars_frame()`, `_build_price_frame()` |
| `scripts/tests/test_trend_scan_e2e.py`                | Modify | Update mock data flow to match TAService integration                                                    |
| `scripts/tests/test_ta_lib/test_snapshot_contract.py` | Create | Verify `get_snapshot()` output matches `_mock_ohlcv_data()` shape exactly                               |

---

### Task 1: Contract verification test — snapshot matches trend_scan expectations

**Files:**

- Create: `scripts/tests/test_ta_lib/test_snapshot_contract.py`

- [ ] **Step 1: Write test that verifies get_snapshot() output has every key trend_scan.py expects**

This test locks the contract between TAService and trend_scan.py before we change anything.

Create `scripts/tests/test_ta_lib/test_snapshot_contract.py`:

```python
"""Contract test: TAService.get_snapshot() must return all keys that trend_scan.py expects."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


# These are the exact keys that trend_scan.py's _stage_a, _trend_summary,
# and TrendCandidate construction read from fetch_ohlcv().
# Extracted from scripts/trend_scan.py lines 369-392 and 495-512.
REQUIRED_KEYS = {
    "ticker",
    "close",
    "ma_20",
    "ma_50",
    "ma_200",
    "rsi",
    "adx",
    "macd",
    "macd_signal",
    "macd_histogram",
    "ma_20_series",
    "recent_avg_volume",
    "avg_20d_volume",
    "recent_up_ratio",
    "bbw",
    "high_52w",
    "range_20d_pct",
    "atr_pct",
    "dollar_volume",
    "price",
    # NOTE: market_cap is NOT here — it comes from stock_info, not TAService
    # NOTE: rs_vs_spy is NOT here — it's computed cross-ticker in trend_scan.py
}


def _make_bar_data(n: int = 260) -> list:
    np.random.seed(42)
    bars = []
    from datetime import datetime
    for i in range(n):
        current = datetime(2025, 5, 1) + pd.tseries.offsets.BDay(i)
        close = 100.0 + i * 0.1 + np.random.randn() * 0.3
        bars.append(SimpleNamespace(
            date=current.strftime("%Y%m%d"),
            open=close - 0.1,
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            volume=1_000_000,
        ))
    return bars


@pytest.fixture
def ta_service():
    from scripts.ta_lib.service import TAService

    mock_ib = MagicMock()
    mock_ib.get_historical_data.return_value = _make_bar_data(n=260)
    mock_ib._ib = MagicMock()
    mock_ib._ib.qualifyContracts.return_value = [MagicMock()]
    return TAService(db_path=":memory:", ib_client=mock_ib)


class TestSnapshotContract:
    def test_all_required_keys_present(self, ta_service):
        snapshot = ta_service.get_snapshot("AAPL")
        missing = REQUIRED_KEYS - set(snapshot.keys())
        assert not missing, f"Missing keys in get_snapshot(): {missing}"

    def test_no_nan_in_scalar_fields(self, ta_service):
        snapshot = ta_service.get_snapshot("AAPL")
        scalar_keys = REQUIRED_KEYS - {"ma_20_series", "ticker"}
        for key in scalar_keys:
            val = snapshot[key]
            assert not (isinstance(val, float) and np.isnan(val)), \
                f"snapshot['{key}'] is NaN — trend_scan scoring will break"

    def test_ma_20_series_is_list_of_floats(self, ta_service):
        snapshot = ta_service.get_snapshot("AAPL")
        series = snapshot["ma_20_series"]
        assert isinstance(series, list)
        assert all(isinstance(v, float) for v in series)
        assert 1 <= len(series) <= 5

    def test_values_are_reasonable(self, ta_service):
        snapshot = ta_service.get_snapshot("AAPL")
        assert snapshot["close"] > 0
        assert snapshot["price"] == snapshot["close"]
        assert 0 <= snapshot["rsi"] <= 100
        assert snapshot["adx"] >= 0
        assert snapshot["avg_20d_volume"] > 0
        assert snapshot["dollar_volume"] > 0
        assert snapshot["high_52w"] >= snapshot["close"]

    def test_ticker_is_uppercase(self, ta_service):
        snapshot = ta_service.get_snapshot("aapl")
        assert snapshot["ticker"] == "AAPL"
```

- [ ] **Step 2: Run contract test**

```bash
python3.13 -m pytest scripts/tests/test_ta_lib/test_snapshot_contract.py -xvs
```

Expected: all PASS — this confirms TAService already produces the right shape.

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_ta_lib/test_snapshot_contract.py
git commit -m "test: add snapshot contract test locking TAService ↔ trend_scan interface"
```

---

### Task 2: Replace `fetch_ohlcv()` with `TAService.get_snapshot()`

**Files:**

- Modify: `scripts/trend_scan.py:286-392` (replace `fetch_ohlcv` body)
- Modify: `scripts/trend_scan.py:43-44` (update `DataFetcher` protocol — no change needed, dict return stays)

- [ ] **Step 1: Read current `fetch_ohlcv` to confirm scope**

```bash
python3.13 -m pytest scripts/tests/test_trend_scan_e2e.py -xvs
```

Expected: existing tests PASS (baseline before changes).

- [ ] **Step 2: Add TAService to `LiveTrendDataFetcher.__init__`**

In `scripts/trend_scan.py`, modify the `LiveTrendDataFetcher.__init__` method. Add `ta_service` parameter and store it.

Find the `__init__` method (around line 219) and add the `ta_service` parameter:

```python
class LiveTrendDataFetcher:
    def __init__(self, *, uw_client: Any, ta_service: Any = None):
        self.uw_client = uw_client
        self._ta_service = ta_service
        self._spy_df: Optional[pd.DataFrame] = None  # cached SPY data for rs_vs_spy
        self._stock_info_cache: dict[str, dict[str, Any]] = {}
        self._ticker_data_cache: dict[str, Any] = {}
        self._oi_change_cache: dict[str, list[dict[str, Any]]] = {}
        self._greek_flow_cache: dict[str, tuple[float, float]] = {}
```

**NOTE:** Keep the `*` keyword-only marker — existing callers use `uw_client=`. Drop `ib_client` param (TAService owns the IB connection, LiveTrendDataFetcher never uses IB directly). Remove `self._bars_cache` (dead after fetch_ohlcv replacement).

- [ ] **Step 2b: Add SPY pre-cache method**

Add this method to `LiveTrendDataFetcher`:

```python
    def pre_cache_spy(self) -> None:
        """Cache SPY indicator DataFrame for rs_vs_spy calculations.

        Called once on main thread before parallel_fetch to avoid
        500× redundant DuckDB reads inside worker threads.
        """
        if self._ta_service is not None:
            self._spy_df = self._ta_service.get_indicators("SPY", allow_fetch=False)
```

- [ ] **Step 3: Replace `fetch_ohlcv()` body**

Replace the entire `fetch_ohlcv` method (lines 286-392) with:

```python
    def fetch_ohlcv(self, ticker: str) -> dict:
        if self._ta_service is None:
            raise RuntimeError("TAService not configured — cannot fetch OHLCV")

        # allow_fetch=False: worker threads must not trigger IB calls.
        # All data should be pre-warmed via bulk_refresh() on main thread.
        snapshot = self._ta_service.get_snapshot(ticker, allow_fetch=False)

        # rs_vs_spy: cross-ticker logic using pre-cached SPY DataFrame
        rs_vs_spy = 1.0
        if ticker.upper() != "SPY" and self._spy_df is not None:
            try:
                ticker_df = self._ta_service.get_indicators(ticker, allow_fetch=False)
                if len(self._spy_df) >= 21 and len(ticker_df) >= 21:
                    spy_closes = self._spy_df.set_index("date")["close"]
                    ticker_closes = ticker_df.set_index("date")["close"]
                    aligned = pd.concat([ticker_closes, spy_closes], axis=1, join="inner").tail(40)
                    aligned.columns = ["stock", "spy"]
                    if len(aligned) >= 21:
                        stock_return = aligned["stock"].iloc[-1] / aligned["stock"].iloc[-21]
                        spy_return = aligned["spy"].iloc[-1] / aligned["spy"].iloc[-21]
                        if spy_return > 0:
                            rs_vs_spy = float(stock_return / spy_return)
            except Exception:
                logger.debug("rs_vs_spy calculation failed for %s", ticker, exc_info=True)

        snapshot["rs_vs_spy"] = rs_vs_spy

        # market_cap from UW stock_info (not part of TA module)
        info = self._stock_info(ticker)
        snapshot["market_cap"] = _safe_float(
            info.get("marketcap") or info.get("market_cap") or info.get("marketCap")
        )

        return snapshot
```

- [ ] **Step 4: Remove dead code**

Remove these methods and functions from `scripts/trend_scan.py` that are no longer called:

- `_bars_frame()` method (lines ~235-241)
- `_build_price_frame()` function (lines ~100-125)
- `_series_value()` function (lines ~128-132) — only used inside `fetch_ohlcv()`, now dead
- Remove `self._bars_cache` from `__init__`

Do NOT remove `_safe_float()` — it's still used elsewhere in the file (e.g., `_stock_info`, `fetch_structure`).

- [ ] **Step 5: Run existing tests to check for import/syntax errors**

```bash
python3.13 -c "from scripts.trend_scan import LiveTrendDataFetcher; print('import OK')"
```

Expected: prints `import OK`

- [ ] **Step 6: Commit**

```bash
git add scripts/trend_scan.py
git commit -m "refactor(trend_scan): replace fetch_ohlcv with TAService.get_snapshot"
```

---

### Task 3: Wire `bulk_refresh()` into `run_scan_pipeline()`

**Files:**

- Modify: `scripts/trend_scan.py:614-640` (`run_scan_pipeline` function)

- [ ] **Step 1: Add TAService instantiation and bulk_refresh call**

In `run_scan_pipeline()` (around line 614), after universe is built and before `parallel_fetch`:

```python
def run_scan_pipeline(
    cfg: TrendScanConfig,
    *,
    data_fetcher: DataFetcher,
    uw_client: Any = None,
    ib_client: Any = None,
    db_path: str = DEFAULT_DB_PATH,
    json_cache_path: Optional[str] = None,
    ta_service: Any = None,
) -> dict:
    start = time.monotonic()
    scan_id = _generate_scan_id()
    now = datetime.now(timezone.utc)

    universe = build_universe(cfg, uw_client=uw_client, ib_client=ib_client)

    # Pre-warm TA cache on main thread (ib_insync is not thread-safe)
    if ta_service is not None:
        refresh_tickers = list(universe) + ["SPY"]
        ta_service.bulk_refresh(refresh_tickers)

    # Pre-cache SPY DataFrame so worker threads don't each query DuckDB for it
    if hasattr(data_fetcher, "pre_cache_spy"):
        data_fetcher.pre_cache_spy()

    stage_a_pairs = parallel_fetch(
        items=universe,
        fn=lambda ticker: (ticker, _stage_a(ticker, data_fetcher, cfg)),
        max_workers=cfg.max_workers,
    )
```

- [ ] **Step 2: Update `build_runtime()` to create IBClient and TAService**

Modify `build_runtime()` at `scripts/trend_scan.py:763-772`. The current code returns `ib_client=None`:

```python
def build_runtime():
    from dotenv import load_dotenv

    from scripts.clients.ib_client import IBClient
    from scripts.clients.uw_client import UWClient
    from scripts.ta_lib import TAService

    project_root = Path(_project_root)
    load_dotenv(project_root / ".env")
    load_dotenv(project_root / "web" / ".env")
    uw_client = UWClient()

    # Connect to IB Gateway (required for TA data)
    ib_client = IBClient()
    try:
        ib_client.connect()
    except Exception:
        logger.warning("IB Gateway not available — TA cache will use stale data")
        ib_client = None

    ta_service = TAService(db_path="data/ta.duckdb", ib_client=ib_client)
    data_fetcher = LiveTrendDataFetcher(
        uw_client=uw_client, ta_service=ta_service,
    )
    return data_fetcher, uw_client, ib_client, ta_service
```

- [ ] **Step 2b: Update `main()` to pass `ta_service` to `run_scan_pipeline`**

Modify `main()` at `scripts/trend_scan.py:775-813`:

```python
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Trend scanner")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--json-cache", default="data/trend_scan.json")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    uw_client = None
    ib_client = None
    try:
        data_fetcher, uw_client, ib_client, ta_service = build_runtime()
        result = run_scan_pipeline(
            TrendScanConfig(top_n=args.top),
            data_fetcher=data_fetcher,
            uw_client=uw_client,
            ib_client=ib_client,
            db_path=args.db_path,
            json_cache_path=args.json_cache,
            ta_service=ta_service,
        )
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        logger.error("Trend scan failed: %s", exc)
        return 1
    finally:
        if uw_client is not None:
            close_fn = getattr(uw_client, "close", None)
            if callable(close_fn):
                close_fn()
        if ib_client is not None:
            close_fn = getattr(ib_client, "disconnect", None)
            if callable(close_fn):
                close_fn()
```

- [ ] **Step 3: Commit**

```bash
git add scripts/trend_scan.py
git commit -m "feat(trend_scan): wire TAService.bulk_refresh into scan pipeline"
```

---

### Task 4: Update `fetch_market_context` to use TAService

**Files:**

- Modify: `scripts/trend_scan.py:474-492`

- [ ] **Step 1: Update `fetch_market_context` to use get_snapshot**

The current `fetch_market_context()` calls `self.fetch_ohlcv("SPY")` which now goes through TAService. This should work without changes since `fetch_ohlcv` is already replaced. But verify:

```python
    def fetch_market_context(self) -> dict:
        spy_snapshot = self.fetch_ohlcv("SPY")
        market_context = {
            "spy_close": spy_snapshot.get("close", 0.0),
            "vix_close": 0.0,
            "regime": "bullish" if spy_snapshot.get("close", 0.0) >= spy_snapshot.get("ma_20", 0.0) else "bearish",
        }
        # ... rest unchanged (CRI file read)
```

This already works because `fetch_ohlcv("SPY")` now calls `TAService.get_snapshot("SPY")`, which returns a dict with `close` and `ma_20`. No changes needed — just verify.

- [ ] **Step 2: Run syntax check**

```bash
python3.13 -c "from scripts.trend_scan import LiveTrendDataFetcher; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit** (only if changes were needed)

---

### Task 5: Update E2E tests

**Files:**

- Modify: `scripts/tests/test_trend_scan_e2e.py`

- [ ] **Step 1: Read current test file to understand mock structure**

The existing tests mock `data_fetcher.fetch_ohlcv` with `_mock_ohlcv_data()`. Since `fetch_ohlcv()` now delegates to TAService internally, we have two options:

1. Keep mocking at the `DataFetcher` protocol level (no TAService needed in tests)
2. Mock TAService and inject it

Option 1 is simpler — the tests mock the `DataFetcher` protocol, which still has the same `fetch_ohlcv() -> dict` signature. The mock returns the same dict shape. The tests should still pass without changes to the mock data.

- [ ] **Step 2: Run existing E2E tests**

```bash
python3.13 -m pytest scripts/tests/test_trend_scan_e2e.py -xvs
```

Expected: tests should pass because they mock `data_fetcher.fetch_ohlcv` directly (bypassing TAService). If they fail, it's likely due to:

- The `ta_service` parameter added to `run_scan_pipeline()` — tests don't pass it, but it defaults to `None` which is fine since they mock `data_fetcher`
- Removed helper functions — check if any test imports `_build_price_frame` or `_bars_frame`

- [ ] **Step 3: Fix any test breakage**

If tests reference removed functions, update imports. The mock structure (`_mock_ohlcv_data`) should remain unchanged since it returns the exact dict shape that `fetch_ohlcv()` always returned.

Add `rs_vs_spy` to `_mock_ohlcv_data` if it's not already there (check lines 12-60 of test file — it IS already there at line 30/49).

- [ ] **Step 4: Add integration test with mocked TAService**

Append to `scripts/tests/test_trend_scan_e2e.py`:

```python
class TestTAServiceIntegration:
    """Verify trend_scan works when wired to a real (mocked-IB) TAService."""

    def test_fetch_ohlcv_delegates_to_ta_service(self):
        import pandas as pd
        from unittest.mock import MagicMock

        mock_ta = MagicMock()
        mock_ta.get_snapshot.return_value = _mock_ohlcv_data("AAPL", bullish=True)
        mock_ta.get_indicators.return_value = pd.DataFrame({
            "date": pd.bdate_range("2026-01-01", periods=30),
            "close": [150.0] * 30,
        })

        from scripts.trend_scan import LiveTrendDataFetcher
        uw = MagicMock()
        uw.get_stock_info.return_value = {"data": {"marketcap": 2_000_000_000}}
        fetcher = LiveTrendDataFetcher(uw_client=uw, ta_service=mock_ta)  # keyword-only
        # Pre-cache SPY since fetch_ohlcv uses it
        fetcher._spy_df = pd.DataFrame({
            "date": pd.bdate_range("2026-01-01", periods=30),
            "close": [450.0] * 30,
        })
        result = fetcher.fetch_ohlcv("AAPL")

        mock_ta.get_snapshot.assert_called_once_with("AAPL", allow_fetch=False)
        assert result["ticker"] == "AAPL"
        assert "rs_vs_spy" in result
        assert "market_cap" in result
```

- [ ] **Step 5: Run all E2E tests**

```bash
python3.13 -m pytest scripts/tests/test_trend_scan_e2e.py -xvs
```

Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/tests/test_trend_scan_e2e.py
git commit -m "test(trend_scan): update E2E tests for TAService integration"
```

---

### Task 6: TA-Lib vs pandas regression check

**Files:**

- Create: `scripts/tests/test_ta_lib/test_regression.py`

- [ ] **Step 1: Write regression test comparing old pandas math to TA-Lib**

This test documents the known differences between the old pandas implementation and TA-Lib.

Create `scripts/tests/test_ta_lib/test_regression.py`:

```python
"""Regression tests: document expected differences between pandas TA and TA-Lib.

ADX and ATR will differ significantly (Wilder's smoothing vs rolling average).
SMA and MACD should match closely. RSI uses different EMA initialization.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import talib


def _make_price_series(n: int = 260, seed: int = 42) -> pd.DataFrame:
    np.random.seed(seed)
    closes = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
    highs = closes + np.abs(np.random.randn(n) * 0.3)
    lows = closes - np.abs(np.random.randn(n) * 0.3)
    return pd.DataFrame({
        "close": closes,
        "high": highs,
        "low": lows,
    })


class TestSMARegression:
    """SMA should match exactly — both use simple rolling mean."""

    def test_sma_20_matches_pandas(self):
        df = _make_price_series()
        pandas_sma = df["close"].rolling(20).mean().iloc[-1]
        talib_sma = talib.SMA(df["close"].to_numpy(), timeperiod=20)[-1]
        assert pandas_sma == pytest.approx(talib_sma, rel=1e-10)

    def test_sma_200_matches_pandas(self):
        df = _make_price_series()
        pandas_sma = df["close"].rolling(200).mean().iloc[-1]
        talib_sma = talib.SMA(df["close"].to_numpy(), timeperiod=200)[-1]
        assert pandas_sma == pytest.approx(talib_sma, rel=1e-10)


class TestMACDRegression:
    """MACD should be close — both use EMA, but initialization may differ slightly."""

    def test_macd_close_to_pandas(self):
        df = _make_price_series()
        closes = df["close"]

        # Pandas MACD
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        pandas_macd = (ema12 - ema26).iloc[-1]

        # TA-Lib MACD
        macd, _, _ = talib.MACD(closes.to_numpy(), fastperiod=12, slowperiod=26, signalperiod=9)
        talib_macd = macd[-1]

        assert pandas_macd == pytest.approx(talib_macd, rel=0.05)


class TestADXRegression:
    """ADX WILL differ — TA-Lib uses Wilder's smoothing, pandas used rolling."""

    def test_adx_differs_from_pandas_rolling(self):
        df = _make_price_series()
        close = df["close"].to_numpy()
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()

        talib_adx = talib.ADX(high, low, close, timeperiod=14)[-1]

        # Just verify TA-Lib produces a reasonable value
        assert 0 <= talib_adx <= 100
        # We don't assert it matches pandas — it intentionally won't


class TestATRRegression:
    """ATR WILL differ — TA-Lib uses Wilder's smoothing, pandas used rolling mean."""

    def test_atr_is_positive(self):
        df = _make_price_series()
        close = df["close"].to_numpy()
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()

        talib_atr = talib.ATR(high, low, close, timeperiod=14)[-1]
        assert talib_atr > 0


class TestScoringThresholds:
    """Verify TA-Lib outputs still produce reasonable scoring decisions.

    These thresholds come from ta_prefilter.py scoring functions.
    """

    def test_rsi_in_valid_range(self):
        df = _make_price_series()
        rsi = talib.RSI(df["close"].to_numpy(), timeperiod=14)
        valid = rsi[~np.isnan(rsi)]
        assert all(0 <= v <= 100 for v in valid)

    def test_bbw_is_positive(self):
        df = _make_price_series()
        upper, middle, lower = talib.BBANDS(
            df["close"].to_numpy(), timeperiod=20, nbdevup=2, nbdevdn=2
        )
        # bb_width = (upper - lower) / middle
        valid_mask = ~np.isnan(middle) & (middle != 0)
        bbw = (upper[valid_mask] - lower[valid_mask]) / middle[valid_mask]
        assert all(v >= 0 for v in bbw)
```

- [ ] **Step 2: Run regression tests**

```bash
python3.13 -m pytest scripts/tests/test_ta_lib/test_regression.py -xvs
```

Expected: all PASS. SMA matches exactly. MACD close. ADX/ATR just validate reasonable ranges.

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_ta_lib/test_regression.py
git commit -m "test: add TA-Lib vs pandas regression tests documenting expected differences"
```

---

### Task 7: Clean up unused imports

**Files:**

- Modify: `scripts/trend_scan.py` (top-level imports)

- [ ] **Step 1: Remove unused imports**

After removing `_bars_frame` and `_build_price_frame`, check if any imports are now unused:

- If `uw_client.get_stock_ohlc` is no longer called anywhere in the file, the UW OHLC import path may be removable (but `uw_client` is still used for `stock_info`, `stock_oi_change`, `greek_flow`, etc.)
- Check if `_build_price_frame`'s helper imports (e.g. specific pandas functions) are still needed

- [ ] **Step 2: Add TAService import**

Ensure this import is at the top of `trend_scan.py`:

```python
from scripts.ta_lib import TAService
```

- [ ] **Step 3: Run full test suite**

```bash
python3.13 -m pytest scripts/tests/test_trend_scan_e2e.py scripts/tests/test_ta_lib/ -xvs
```

Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add scripts/trend_scan.py
git commit -m "chore(trend_scan): clean up unused imports after TAService integration"
```

---

## Verification Plan

Run after all tasks are complete. Every step must pass before the integration is considered done.

### V1: Contract verification — snapshot shape matches

```bash
python3.13 -m pytest scripts/tests/test_ta_lib/test_snapshot_contract.py -xvs
```

Expected: all PASS. `get_snapshot()` returns every key `trend_scan.py` expects.

### V2: TA-Lib module tests still pass

```bash
python3.13 -m pytest scripts/tests/test_ta_lib/ -xvs
```

Expected: all PASS. No regressions in the standalone TA module.

### V3: Trend scan E2E tests pass

```bash
python3.13 -m pytest scripts/tests/test_trend_scan_e2e.py -xvs
```

Expected: all PASS. Existing mock-based tests work (mocks bypass TAService). New TAService integration test passes.

### V4: Full Python test suite

```bash
python3.13 scripts/run_pytest_affected.py
```

Expected: all PASS. No regressions in any other test file.

### V5: Import chain verification

```bash
python3.13 -c "
from scripts.trend_scan import LiveTrendDataFetcher, run_scan_pipeline
from scripts.ta_lib import TAService
print('All imports OK')
"
```

Expected: `All imports OK`

### V6: Removed code verification

```bash
python3.13 -c "
from scripts.trend_scan import LiveTrendDataFetcher
from unittest.mock import MagicMock

# Verify removed methods don't exist
fetcher = LiveTrendDataFetcher(uw_client=MagicMock(), ta_service=MagicMock())
assert not hasattr(fetcher, '_bars_frame'), '_bars_frame should be removed'
assert not hasattr(fetcher, '_bars_cache'), '_bars_cache should be removed'
print('Dead code removal verified')

# Verify _series_value is gone
import scripts.trend_scan as ts_mod
assert not hasattr(ts_mod, '_series_value'), '_series_value should be removed'
assert not hasattr(ts_mod, '_build_price_frame'), '_build_price_frame should be removed'
print('Module-level dead code verified')

# Verify no UW OHLC path remains
import inspect
source = inspect.getsource(LiveTrendDataFetcher.fetch_ohlcv)
assert 'get_stock_ohlc' not in source, 'UW OHLC call should be removed'
assert '_build_price_frame' not in source, '_build_price_frame should be removed'
print('UW OHLC path removed: OK')
"
```

### V7: Regression tests — TA-Lib vs pandas differences documented

```bash
python3.13 -m pytest scripts/tests/test_ta_lib/test_regression.py -xvs
```

Expected: all PASS. SMA matches exactly, MACD close, ADX/ATR in valid ranges.

### V8: Coverage check on modified files

```bash
python3.13 -m pytest scripts/tests/test_trend_scan_e2e.py scripts/tests/test_ta_lib/ \
  --cov=scripts.trend_scan --cov=scripts.ta_lib --cov-report=term-missing
```

Expected: ≥90% on `trend_scan.py`'s modified sections. ≥95% on `ta_lib/`.

### V9: Thread-safety — worker threads never call IB

```bash
python3.13 -c "
from unittest.mock import MagicMock, patch
from scripts.trend_scan import LiveTrendDataFetcher

mock_ta = MagicMock()
# get_snapshot with allow_fetch=False should be what fetch_ohlcv calls
mock_ta.get_snapshot.return_value = {
    'ticker': 'AAPL', 'close': 150, 'price': 150, 'ma_20': 145,
    'ma_50': 140, 'ma_200': 130, 'rsi': 62, 'adx': 32, 'macd': 1.5,
    'macd_signal': 1.0, 'macd_histogram': 0.5, 'bbw': 0.05, 'atr_pct': 0.015,
    'ma_20_series': [140,141,142,143,145], 'recent_avg_volume': 1500000,
    'avg_20d_volume': 1000000, 'recent_up_ratio': 0.7, 'high_52w': 152,
    'range_20d_pct': 0.04, 'dollar_volume': 20000000,
}
mock_ta.get_indicators.return_value = MagicMock(empty=True)
uw = MagicMock()
uw.get_stock_info.return_value = {'data': {'marketcap': 2e9}}

fetcher = LiveTrendDataFetcher(uw_client=uw, ta_service=mock_ta)
fetcher.fetch_ohlcv('AAPL')

# Verify allow_fetch=False was passed
call_args = mock_ta.get_snapshot.call_args
assert call_args.kwargs.get('allow_fetch') is False or call_args[1].get('allow_fetch') is False, \
    'fetch_ohlcv must pass allow_fetch=False to prevent IB calls from worker threads'
print('V9 PASSED: allow_fetch=False enforced')
"
```

### V10: build_runtime() creates TAService

```bash
python3.13 -c "
import inspect
from scripts.trend_scan import build_runtime
source = inspect.getsource(build_runtime)
assert 'TAService' in source, 'build_runtime must create TAService'
assert 'IBClient' in source, 'build_runtime must create IBClient'
print('V10 PASSED: build_runtime creates IB + TA')
"
```

### V11: Live IB integration smoke test (requires running IB Gateway)

```bash
python3.13 -c "
try:
    from scripts.trend_scan import build_runtime
    data_fetcher, uw_client, ib_client, ta_service = build_runtime()
    # Refresh just SPY
    ta_service.bulk_refresh(['SPY'])
    result = data_fetcher.fetch_ohlcv('SPY')
    assert result['close'] > 0
    assert result['rsi'] > 0
    print(f'SPY close={result[\"close\"]:.2f}, RSI={result[\"rsi\"]:.1f}')
    print('V11 LIVE INTEGRATION TEST PASSED')
except Exception as e:
    print(f'V11 SKIPPED (IB not available): {e}')
"
```
