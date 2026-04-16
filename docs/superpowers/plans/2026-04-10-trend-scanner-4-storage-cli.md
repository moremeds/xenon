# Sub-Plan 4: DuckDB Storage + CLI Entry Point

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the DuckDB storage layer for scan history, the real `DataFetcher` adapter wrapping `UWClient`, and the `trend_scan.py` CLI entry point that wires the full 3-stage pipeline together.

**Architecture:** `trend_scan_lib/storage.py` manages DuckDB schema creation and inserts. `trend_scan_lib/data_fetcher.py` implements the `DataFetcher` protocol using `UWClient`. `trend_scan.py` orchestrates: build universe → Stage A filter (parallel) → Stage B/C score → rank → write DuckDB + JSON cache. CLI supports `--top N` flag, outputs JSON to stdout for FastAPI subprocess integration.

**Tech Stack:** Python 3.14, pytest, DuckDB, argparse

**Spec:** `docs/superpowers/specs/2026-04-10-trend-scanner-design.md` (Storage + Output Schema)

**Depends on:** Sub-Plans 1-3 must be complete.

---

## File Structure

```
scripts/
├── trend_scan_lib/
│   ├── storage.py               # CREATE — DuckDB schema + writer
│   └── data_fetcher.py          # CREATE — DataFetcher adapter wrapping UWClient
├── trend_scan.py                 # CREATE — CLI entry point + pipeline orchestrator
└── tests/
    ├── test_trend_storage.py     # CREATE
    ├── test_data_fetcher.py      # CREATE
    └── test_trend_scan_e2e.py    # CREATE
```

---

### Task 1: DuckDB Storage (`trend_scan_lib/storage.py`)

**Files:**

- Create: `scripts/trend_scan_lib/storage.py`
- Test: `scripts/tests/test_trend_storage.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_trend_storage.py
"""Tests for trend scanner DuckDB storage."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


def test_init_schema_creates_tables():
    import duckdb

    from scripts.trend_scan_lib.storage import init_schema

    conn = duckdb.connect(":memory:")
    init_schema(conn)

    tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
    table_names = {t[0] for t in tables}
    assert "scan_runs" in table_names
    assert "scan_candidates" in table_names


def test_init_schema_idempotent():
    import duckdb

    from scripts.trend_scan_lib.storage import init_schema

    conn = duckdb.connect(":memory:")
    init_schema(conn)
    init_schema(conn)  # should not raise

    count = conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    assert count == 0


def test_write_scan_run():
    import duckdb

    from scripts.trend_scan_lib.storage import init_schema, write_scan_run

    conn = duckdb.connect(":memory:")
    init_schema(conn)

    run = {
        "scan_id": "trend_20260410_0845",
        "scan_timestamp": datetime(2026, 4, 10, 8, 45, 0, tzinfo=timezone.utc),
        "universe_size": 743,
        "stage_a_pass": 187,
        "stage_b_pass": 92,
        "candidates_out": 25,
        "spy_close": 523.45,
        "vix_close": 18.2,
        "regime": "bullish",
        "duration_secs": 42.5,
    }
    write_scan_run(conn, run)

    row = conn.execute("SELECT scan_id, universe_size, regime FROM scan_runs").fetchone()
    assert row[0] == "trend_20260410_0845"
    assert row[1] == 743
    assert row[2] == "bullish"


def test_write_scan_candidate():
    import duckdb

    from scripts.trend_scan_lib.storage import init_schema, write_scan_candidates, write_scan_run

    conn = duckdb.connect(":memory:")
    init_schema(conn)

    # Must write run first (FK)
    run = {
        "scan_id": "test_001",
        "scan_timestamp": datetime(2026, 4, 10, 8, 45, 0, tzinfo=timezone.utc),
        "universe_size": 100,
        "stage_a_pass": 50,
        "stage_b_pass": 30,
        "candidates_out": 10,
        "spy_close": 520.0,
        "vix_close": 17.0,
        "regime": "bullish",
        "duration_secs": 30.0,
    }
    write_scan_run(conn, run)

    candidates = [
        {
            "scan_id": "test_001",
            "ticker": "NVDA",
            "snapshot_timestamp": datetime(2026, 4, 10, 8, 45, 0, tzinfo=timezone.utc),
            "spot_price": 148.30,
            "direction": "bullish",
            "final_score": 0.82,
            "trend_score": 0.91,
            "structure_score": 0.75,
            "vol_score": 0.68,
            "flow_score": 0.85,
            "ma_20": 142.50,
            "ma_50": 138.20,
            "ma_200": 125.80,
            "rsi": 62.3,
            "adx": 32.1,
            "macd_histogram": 1.45,
            "bbw": 0.08,
            "rs_vs_spy": 1.15,
            "iv_rank": 22.0,
            "gamma_flip": 145.0,
            "call_wall": 160.0,
            "put_wall": 140.0,
            "suggested_trade": "debit_call",
            "invalidation": 142.50,
            "flags": ["breakout"],
            "trend_summary": "Full MA stack",
            "structure_summary": "Above gamma flip",
            "vol_summary": "IV rank 22",
            "flow_summary": "4 ask-side prints",
        },
    ]
    write_scan_candidates(conn, candidates)

    row = conn.execute("SELECT ticker, final_score, rsi, flags FROM scan_candidates").fetchone()
    assert row[0] == "NVDA"
    assert abs(row[1] - 0.82) < 0.01
    assert abs(row[2] - 62.3) < 0.1
    assert row[3] == ["breakout"]


def test_write_multiple_candidates():
    import duckdb

    from scripts.trend_scan_lib.storage import init_schema, write_scan_candidates, write_scan_run

    conn = duckdb.connect(":memory:")
    init_schema(conn)

    run = {
        "scan_id": "test_002",
        "scan_timestamp": datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc),
        "universe_size": 100, "stage_a_pass": 50, "stage_b_pass": 30,
        "candidates_out": 2, "spy_close": 520.0, "vix_close": 17.0,
        "regime": "bullish", "duration_secs": 25.0,
    }
    write_scan_run(conn, run)

    candidates = [
        _make_candidate("test_002", "AAPL", 0.9),
        _make_candidate("test_002", "MSFT", 0.8),
    ]
    write_scan_candidates(conn, candidates)

    count = conn.execute("SELECT COUNT(*) FROM scan_candidates WHERE scan_id='test_002'").fetchone()[0]
    assert count == 2


def test_query_historical_scores():
    import duckdb

    from scripts.trend_scan_lib.storage import init_schema, write_scan_candidates, write_scan_run

    conn = duckdb.connect(":memory:")
    init_schema(conn)

    # Two scan runs
    for sid, ts_hour in [("run_1", 8), ("run_2", 9)]:
        write_scan_run(conn, {
            "scan_id": sid,
            "scan_timestamp": datetime(2026, 4, 10, ts_hour, 0, 0, tzinfo=timezone.utc),
            "universe_size": 100, "stage_a_pass": 50, "stage_b_pass": 30,
            "candidates_out": 1, "spy_close": 520.0, "vix_close": 17.0,
            "regime": "bullish", "duration_secs": 20.0,
        })
        write_scan_candidates(conn, [_make_candidate(sid, "NVDA", 0.85)])

    rows = conn.execute(
        "SELECT r.scan_id, c.ticker, c.final_score "
        "FROM scan_candidates c JOIN scan_runs r ON c.scan_id = r.scan_id "
        "ORDER BY r.scan_timestamp"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "run_1"
    assert rows[1][0] == "run_2"


def test_file_based_db(tmp_path):
    import duckdb

    from scripts.trend_scan_lib.storage import get_connection, init_schema

    db_path = tmp_path / "test.duckdb"
    conn = get_connection(str(db_path))
    init_schema(conn)

    assert db_path.exists()
    conn.close()

    # Reopen and verify schema persists
    conn2 = get_connection(str(db_path))
    tables = conn2.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
    assert len(tables) >= 2
    conn2.close()


# --- Helper ---

def _make_candidate(scan_id: str, ticker: str, score: float) -> dict:
    return {
        "scan_id": scan_id,
        "ticker": ticker,
        "snapshot_timestamp": datetime(2026, 4, 10, 8, 45, 0, tzinfo=timezone.utc),
        "spot_price": 100.0,
        "direction": "bullish",
        "final_score": score,
        "trend_score": score,
        "structure_score": score * 0.9,
        "vol_score": score * 0.8,
        "flow_score": score * 0.7,
        "ma_20": 95.0, "ma_50": 90.0, "ma_200": 80.0,
        "rsi": 60.0, "adx": 30.0, "macd_histogram": 1.0,
        "bbw": 0.05, "rs_vs_spy": 1.1, "iv_rank": 25.0,
        "gamma_flip": 98.0, "call_wall": 110.0, "put_wall": 95.0,
        "suggested_trade": "debit_call",
        "invalidation": 95.0,
        "flags": [],
        "trend_summary": "Good",
        "structure_summary": "Good",
        "vol_summary": "Good",
        "flow_summary": "Good",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_storage.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement DuckDB storage**

```python
# scripts/trend_scan_lib/storage.py
"""DuckDB storage for trend scan history and backtesting."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/trend_scan.duckdb"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scan_runs (
    scan_id        VARCHAR PRIMARY KEY,
    scan_timestamp TIMESTAMPTZ NOT NULL,
    universe_size  INTEGER,
    stage_a_pass   INTEGER,
    stage_b_pass   INTEGER,
    candidates_out INTEGER,
    spy_close      DOUBLE,
    vix_close      DOUBLE,
    regime         VARCHAR,
    duration_secs  DOUBLE
);

CREATE TABLE IF NOT EXISTS scan_candidates (
    scan_id            VARCHAR NOT NULL,
    ticker             VARCHAR NOT NULL,
    snapshot_timestamp  TIMESTAMPTZ NOT NULL,
    spot_price         DOUBLE,
    direction          VARCHAR,
    final_score        DOUBLE,
    trend_score        DOUBLE,
    structure_score    DOUBLE,
    vol_score          DOUBLE,
    flow_score         DOUBLE,
    ma_20              DOUBLE,
    ma_50              DOUBLE,
    ma_200             DOUBLE,
    rsi                DOUBLE,
    adx                DOUBLE,
    macd_histogram     DOUBLE,
    bbw                DOUBLE,
    rs_vs_spy          DOUBLE,
    iv_rank            DOUBLE,
    gamma_flip         DOUBLE,
    call_wall          DOUBLE,
    put_wall           DOUBLE,
    suggested_trade    VARCHAR,
    invalidation       DOUBLE,
    flags              VARCHAR[],
    holding_window     VARCHAR,
    trend_summary      VARCHAR,
    structure_summary  VARCHAR,
    vol_summary        VARCHAR,
    flow_summary       VARCHAR,
    PRIMARY KEY (scan_id, ticker),
    FOREIGN KEY (scan_id) REFERENCES scan_runs(scan_id)
);
"""


def get_connection(db_path: str = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    """Open or create a DuckDB connection."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(db_path)


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables if they don't exist."""
    conn.execute(SCHEMA_SQL)


def write_scan_run(conn: duckdb.DuckDBPyConnection, run: dict[str, Any]) -> None:
    """Insert a scan run record."""
    conn.execute(
        """INSERT INTO scan_runs (
            scan_id, scan_timestamp, universe_size, stage_a_pass, stage_b_pass,
            candidates_out, spy_close, vix_close, regime, duration_secs
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            run["scan_id"],
            run["scan_timestamp"],
            run.get("universe_size", 0),
            run.get("stage_a_pass", 0),
            run.get("stage_b_pass", 0),
            run.get("candidates_out", 0),
            run.get("spy_close", 0),
            run.get("vix_close", 0),
            run.get("regime", "unknown"),
            run.get("duration_secs", 0),
        ],
    )


def write_scan_candidates(
    conn: duckdb.DuckDBPyConnection,
    candidates: list[dict[str, Any]],
) -> None:
    """Insert candidate records for a scan run."""
    for c in candidates:
        conn.execute(
            """INSERT INTO scan_candidates (
                scan_id, ticker, snapshot_timestamp, spot_price, direction,
                final_score, trend_score, structure_score, vol_score, flow_score,
                ma_20, ma_50, ma_200, rsi, adx, macd_histogram, bbw, rs_vs_spy,
                iv_rank, gamma_flip, call_wall, put_wall,
                suggested_trade, invalidation, flags, holding_window,
                trend_summary, structure_summary, vol_summary, flow_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                c["scan_id"], c["ticker"], c["snapshot_timestamp"],
                c.get("spot_price", 0), c.get("direction", "bullish"),
                c.get("final_score", 0), c.get("trend_score", 0),
                c.get("structure_score", 0), c.get("vol_score", 0), c.get("flow_score", 0),
                c.get("ma_20", 0), c.get("ma_50", 0), c.get("ma_200", 0),
                c.get("rsi", 0), c.get("adx", 0), c.get("macd_histogram", 0),
                c.get("bbw", 0), c.get("rs_vs_spy", 0),
                c.get("iv_rank", 0), c.get("gamma_flip", 0),
                c.get("call_wall", 0), c.get("put_wall", 0),
                c.get("suggested_trade", ""), c.get("invalidation", 0),
                c.get("flags", []), c.get("holding_window", ""),
                c.get("trend_summary", ""), c.get("structure_summary", ""),
                c.get("vol_summary", ""), c.get("flow_summary", ""),
            ],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_storage.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/storage.py scripts/tests/test_trend_storage.py
git commit -m "feat(trend_scan_lib): add DuckDB storage for scan history"
```

---

### Task 2: DataFetcher Adapter (`trend_scan_lib/data_fetcher.py`)

**Files:**

- Create: `scripts/trend_scan_lib/data_fetcher.py`
- Test: `scripts/tests/test_data_fetcher.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_data_fetcher.py
"""Tests for UWDataFetcher adapter."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_fetch_ohlcv_returns_dict():
    from scripts.trend_scan_lib.data_fetcher import UWDataFetcher

    mock_uw = MagicMock()
    mock_uw.get_ticker_data.return_value = {
        "close": 150.0, "ma_20": 145.0, "ma_50": 140.0, "ma_200": 130.0,
        "rsi": 62.0, "adx": 32.0, "macd": 1.5, "macd_signal": 1.0,
        "macd_histogram": 0.5, "rs_vs_spy": 1.15,
    }
    fetcher = UWDataFetcher(uw_client=mock_uw)
    result = fetcher.fetch_ohlcv("AAPL")
    assert result["close"] == 150.0
    assert result["rsi"] == 62.0


def test_fetch_structure_returns_dict():
    from scripts.trend_scan_lib.data_fetcher import UWDataFetcher

    mock_uw = MagicMock()
    mock_uw.get_greek_exposure.return_value = {
        "spot": 150, "gamma_flip": 145, "call_wall": 165,
        "put_wall": 146, "max_pain": 148, "net_gex": 200_000,
    }
    fetcher = UWDataFetcher(uw_client=mock_uw)
    result = fetcher.fetch_structure("AAPL")
    assert "gamma_flip" in result


def test_fetch_volatility_returns_dict():
    from scripts.trend_scan_lib.data_fetcher import UWDataFetcher

    mock_uw = MagicMock()
    mock_uw.get_iv_data.return_value = {"iv_rank": 22, "term_structure": "normal"}
    fetcher = UWDataFetcher(uw_client=mock_uw)
    result = fetcher.fetch_volatility("AAPL")
    assert result["iv_rank"] == 22


def test_fetch_flow_returns_dict():
    from scripts.trend_scan_lib.data_fetcher import UWDataFetcher

    mock_uw = MagicMock()
    mock_uw.get_flow_alerts.return_value = [
        {"ticker": "AAPL", "side": "ask", "premium": 500_000},
    ]
    fetcher = UWDataFetcher(uw_client=mock_uw)
    result = fetcher.fetch_flow("AAPL")
    assert "ask_dominance" in result or "flow_count" in result


def test_fetch_market_context_returns_dict():
    from scripts.trend_scan_lib.data_fetcher import UWDataFetcher

    mock_uw = MagicMock()
    mock_uw.get_market_overview.return_value = {
        "spy_close": 520.0, "vix_close": 17.0,
    }
    fetcher = UWDataFetcher(uw_client=mock_uw)
    result = fetcher.fetch_market_context()
    assert "spy_close" in result
    assert "regime" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_data_fetcher.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement DataFetcher adapter**

```python
# scripts/trend_scan_lib/data_fetcher.py
"""DataFetcher adapter wrapping UWClient for the trend scanner pipeline."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class UWDataFetcher:
    """Adapts UWClient API calls to the DataFetcher protocol expected by trend_scan pipeline."""

    def __init__(self, uw_client: Any) -> None:
        self._uw = uw_client

    def fetch_ohlcv(self, ticker: str) -> dict:
        """Fetch OHLCV + TA indicators for a ticker."""
        return self._uw.get_ticker_data(ticker)

    def fetch_structure(self, ticker: str) -> dict:
        """Fetch options structure (gamma, walls, GEX) for a ticker."""
        return self._uw.get_greek_exposure(ticker)

    def fetch_volatility(self, ticker: str) -> dict:
        """Fetch IV state for a ticker."""
        return self._uw.get_iv_data(ticker)

    def fetch_flow(self, ticker: str) -> dict:
        """Fetch flow alerts and aggregate into flow summary for a ticker."""
        alerts = self._uw.get_flow_alerts(ticker=ticker) or []
        ask_count = sum(1 for a in alerts if a.get("side") == "ask")
        total = len(alerts)
        return {
            "ask_dominance": ask_count / total if total > 0 else 0.5,
            "flow_count": total,
            "expiry_cluster_ratio": 0.5,  # TODO: compute from alert expiry spread
            "avg_strike_pct_otm": 0.05,   # TODO: compute from alert strikes
            "net_delta": sum(a.get("delta", 0) for a in alerts),
            "net_vega": sum(a.get("vega", 0) for a in alerts),
            "dp_direction": "neutral",     # TODO: integrate dark pool data
        }

    def fetch_market_context(self) -> dict:
        """Fetch market overview (SPY, VIX, regime)."""
        try:
            overview = self._uw.get_market_overview()
            spy = overview.get("spy_close", 0)
            vix = overview.get("vix_close", 0)
            regime = "bullish" if vix < 20 else "cautious" if vix < 30 else "fear"
            return {"spy_close": spy, "vix_close": vix, "regime": regime}
        except Exception:
            logger.warning("Failed to fetch market context", exc_info=True)
            return {"spy_close": 0, "vix_close": 0, "regime": "unknown"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_data_fetcher.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/data_fetcher.py scripts/tests/test_data_fetcher.py
git commit -m "feat(trend_scan_lib): add UWDataFetcher adapter for trend scanner pipeline"
```

---

### Task 3: CLI Entry Point (`trend_scan.py`)

**Files:**

- Create: `scripts/trend_scan.py`
- Test: `scripts/tests/test_trend_scan_e2e.py`

- [ ] **Step 1: Write failing E2E test with mocked clients**

```python
# scripts/tests/test_trend_scan_e2e.py
"""End-to-end tests for trend_scan.py pipeline."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


def _mock_ohlcv_data(ticker: str, bullish: bool = True) -> dict:
    """Generate mock OHLCV + indicator data for a ticker."""
    if bullish:
        return {
            "ticker": ticker,
            "close": 150, "ma_20": 145, "ma_50": 140, "ma_200": 130,
            "rsi": 62, "adx": 32,
            "macd": 1.5, "macd_signal": 1.0, "macd_histogram": 0.5,
            "rs_vs_spy": 1.15, "ma_20_series": [140, 141, 142, 143, 145],
            "recent_avg_volume": 1_500_000, "avg_20d_volume": 1_000_000,
            "recent_up_ratio": 0.7, "bbw": 0.05,
            "high_52w": 152, "range_20d_pct": 0.04, "atr_pct": 0.015,
            "dollar_volume": 20_000_000, "market_cap": 2_000_000_000, "price": 150,
        }
    return {
        "ticker": ticker,
        "close": 120, "ma_20": 130, "ma_50": 140, "ma_200": 150,
        "rsi": 35, "adx": 12,
        "macd": -1.0, "macd_signal": 0.5, "macd_histogram": -1.5,
        "rs_vs_spy": 0.85, "ma_20_series": [145, 144, 143, 142, 140],
        "recent_avg_volume": 800_000, "avg_20d_volume": 1_000_000,
        "recent_up_ratio": 0.3, "bbw": 0.18,
        "high_52w": 170, "range_20d_pct": 0.12, "atr_pct": 0.02,
        "dollar_volume": 20_000_000, "market_cap": 2_000_000_000, "price": 120,
    }


def _mock_structure_data() -> dict:
    return {
        "spot": 150, "gamma_flip": 145, "call_wall": 165,
        "put_wall": 146, "max_pain": 148, "net_gex": 200_000,
        "net_call_oi_change": 3000, "net_put_oi_change": -1000,
        "gex_at_spot": 50_000,
    }


def _mock_vol_data() -> dict:
    return {"iv_rank": 22, "term_structure": "normal", "iv_rv_ratio": 0.94}


def _mock_flow_data() -> dict:
    return {
        "ask_dominance": 0.85, "flow_count": 5,
        "expiry_cluster_ratio": 0.8, "avg_strike_pct_otm": 0.04,
        "net_delta": 40_000, "net_vega": 20_000, "dp_direction": "bullish",
    }


def test_scan_pipeline_produces_output(tmp_path):
    from scripts.trend_scan_lib.config import TrendScanConfig
    from scripts.trend_scan import run_scan_pipeline

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["AAPL", "NVDA"]))
    nq.write_text(json.dumps([]))

    cfg = TrendScanConfig(
        top_n=5,
        sp500_path=str(sp),
        nasdaq100_path=str(nq),
    )

    mock_data_fetcher = MagicMock()
    mock_data_fetcher.fetch_ohlcv.side_effect = lambda t: _mock_ohlcv_data(t, bullish=True)
    mock_data_fetcher.fetch_structure.side_effect = lambda t: _mock_structure_data()
    mock_data_fetcher.fetch_volatility.side_effect = lambda t: _mock_vol_data()
    mock_data_fetcher.fetch_flow.side_effect = lambda t: _mock_flow_data()
    mock_data_fetcher.fetch_market_context.return_value = {
        "spy_close": 520.0, "vix_close": 17.0, "regime": "bullish",
    }

    result = run_scan_pipeline(
        cfg,
        data_fetcher=mock_data_fetcher,
        uw_client=None,
        ib_client=None,
        db_path=":memory:",
    )

    assert "scan_id" in result
    assert "scan_timestamp" in result
    assert "candidates" in result
    assert len(result["candidates"]) <= 5
    assert result["universe_size"] == 2


def test_scan_pipeline_filters_weak_tickers(tmp_path):
    from scripts.trend_scan_lib.config import TrendScanConfig
    from scripts.trend_scan import run_scan_pipeline

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["GOOD", "BAD"]))
    nq.write_text(json.dumps([]))

    cfg = TrendScanConfig(top_n=5, sp500_path=str(sp), nasdaq100_path=str(nq))

    mock_data_fetcher = MagicMock()
    mock_data_fetcher.fetch_ohlcv.side_effect = lambda t: _mock_ohlcv_data(t, bullish=(t == "GOOD"))
    mock_data_fetcher.fetch_structure.side_effect = lambda t: _mock_structure_data()
    mock_data_fetcher.fetch_volatility.side_effect = lambda t: _mock_vol_data()
    mock_data_fetcher.fetch_flow.side_effect = lambda t: _mock_flow_data()
    mock_data_fetcher.fetch_market_context.return_value = {
        "spy_close": 520.0, "vix_close": 17.0, "regime": "bullish",
    }

    result = run_scan_pipeline(
        cfg,
        data_fetcher=mock_data_fetcher,
        uw_client=None,
        ib_client=None,
        db_path=":memory:",
    )

    tickers = [c["ticker"] for c in result["candidates"]]
    # BAD ticker should fail bullish gate (close < ma_20)
    assert "BAD" not in tickers


def test_scan_output_has_required_fields(tmp_path):
    from scripts.trend_scan_lib.config import TrendScanConfig
    from scripts.trend_scan import run_scan_pipeline

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["NVDA"]))
    nq.write_text(json.dumps([]))

    cfg = TrendScanConfig(top_n=5, sp500_path=str(sp), nasdaq100_path=str(nq))

    mock_data_fetcher = MagicMock()
    mock_data_fetcher.fetch_ohlcv.side_effect = lambda t: _mock_ohlcv_data(t, bullish=True)
    mock_data_fetcher.fetch_structure.side_effect = lambda t: _mock_structure_data()
    mock_data_fetcher.fetch_volatility.side_effect = lambda t: _mock_vol_data()
    mock_data_fetcher.fetch_flow.side_effect = lambda t: _mock_flow_data()
    mock_data_fetcher.fetch_market_context.return_value = {
        "spy_close": 520.0, "vix_close": 17.0, "regime": "bullish",
    }

    result = run_scan_pipeline(
        cfg,
        data_fetcher=mock_data_fetcher,
        uw_client=None,
        ib_client=None,
        db_path=":memory:",
    )

    # Check top-level fields
    for key in ["scan_id", "scan_timestamp", "market_context", "universe_size",
                "stage_a_survivors", "stage_b_survivors", "candidates"]:
        assert key in result, f"Missing top-level key: {key}"

    if result["candidates"]:
        c = result["candidates"][0]
        for key in ["ticker", "spot_price", "direction", "final_score", "scores",
                     "indicators", "summaries", "suggested_trade", "invalidation",
                     "flags", "holding_window", "snapshot_timestamp"]:
            assert key in c, f"Missing candidate key: {key}"


def test_scan_writes_to_duckdb(tmp_path):
    import duckdb

    from scripts.trend_scan_lib.config import TrendScanConfig
    from scripts.trend_scan import run_scan_pipeline

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["NVDA"]))
    nq.write_text(json.dumps([]))

    db_path = str(tmp_path / "test.duckdb")
    cfg = TrendScanConfig(top_n=5, sp500_path=str(sp), nasdaq100_path=str(nq))

    mock_data_fetcher = MagicMock()
    mock_data_fetcher.fetch_ohlcv.side_effect = lambda t: _mock_ohlcv_data(t, bullish=True)
    mock_data_fetcher.fetch_structure.side_effect = lambda t: _mock_structure_data()
    mock_data_fetcher.fetch_volatility.side_effect = lambda t: _mock_vol_data()
    mock_data_fetcher.fetch_flow.side_effect = lambda t: _mock_flow_data()
    mock_data_fetcher.fetch_market_context.return_value = {
        "spy_close": 520.0, "vix_close": 17.0, "regime": "bullish",
    }

    run_scan_pipeline(
        cfg,
        data_fetcher=mock_data_fetcher,
        uw_client=None,
        ib_client=None,
        db_path=db_path,
    )

    conn = duckdb.connect(db_path)
    runs = conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    candidates = conn.execute("SELECT COUNT(*) FROM scan_candidates").fetchone()[0]
    conn.close()

    assert runs == 1
    assert candidates >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_scan_e2e.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement CLI entry point and pipeline**

```python
# scripts/trend_scan.py
"""Trend scanner — 3-stage pre-market trend scanner for swing trade identification.

Usage:
    python scripts/trend_scan.py --top 25
    python scripts/trend_scan.py --top 10 --db-path data/trend_scan.duckdb

Outputs JSON to stdout (for FastAPI subprocess integration) and writes to DuckDB.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from scripts.scanner_lib.cache import write_json_cache
from scripts.scanner_lib.executor import parallel_fetch
from scripts.scanner_lib.scoring import weighted_composite
from scripts.trend_scan_lib.config import TrendScanConfig
from scripts.trend_scan_lib.models import TrendCandidate
from scripts.trend_scan_lib.ranking import apply_min_thresholds, compute_final_score, rank_candidates
from scripts.trend_scan_lib.stages.flow_confirmation import compute_flow_score
from scripts.trend_scan_lib.stages.options_structure import compute_structure_score
from scripts.trend_scan_lib.stages.ta_prefilter import (
    compute_trend_score,
    passes_bullish_gate,
)
from scripts.trend_scan_lib.stages.volatility import compute_vol_score, suggest_trade_type
from scripts.trend_scan_lib.storage import (
    DEFAULT_DB_PATH,
    get_connection,
    init_schema,
    write_scan_candidates,
    write_scan_run,
)
from scripts.trend_scan_lib.universe import build_universe

logger = logging.getLogger(__name__)


class DataFetcher(Protocol):
    """Protocol for fetching data per ticker. Implemented by real or mock fetchers."""

    def fetch_ohlcv(self, ticker: str) -> dict: ...
    def fetch_structure(self, ticker: str) -> dict: ...
    def fetch_volatility(self, ticker: str) -> dict: ...
    def fetch_flow(self, ticker: str) -> dict: ...
    def fetch_market_context(self) -> dict: ...


def _generate_scan_id() -> str:
    now = datetime.now(timezone.utc)
    return f"trend_{now.strftime('%Y%m%d_%H%M')}"


def _stage_a(ticker: str, data_fetcher: DataFetcher, cfg: TrendScanConfig) -> Optional[dict]:
    """Stage A: TA prefilter. Returns indicator dict or None if gated out."""
    try:
        ohlcv = data_fetcher.fetch_ohlcv(ticker)
    except Exception:
        logger.warning("Failed to fetch OHLCV for %s", ticker, exc_info=True)
        return None

    if not passes_bullish_gate(
        close=ohlcv.get("close", 0),
        ma_20=ohlcv.get("ma_20", 0),
        rsi=ohlcv.get("rsi", 0),
        dollar_volume=ohlcv.get("dollar_volume", 0),
        min_dollar_volume=cfg.min_dollar_volume,
    ):
        return None

    trend_score = compute_trend_score(ohlcv)
    ohlcv["trend_score"] = trend_score
    return ohlcv


def _stage_bc(ticker: str, ohlcv: dict, data_fetcher: DataFetcher) -> Optional[dict]:
    """Stages B+C: structure, volatility, flow. Returns combined scores or None if rejected."""
    try:
        struct_data = data_fetcher.fetch_structure(ticker)
        structure_score, rejected = compute_structure_score(struct_data)
        if rejected:
            return None

        vol_data = data_fetcher.fetch_volatility(ticker)
        vol_score, vol_flags = compute_vol_score(vol_data)

        flow_data = data_fetcher.fetch_flow(ticker)
        flow_score = compute_flow_score(flow_data)

        # Trade type suggestion
        capped = struct_data.get("call_wall", 0) > 0 and (
            (struct_data["call_wall"] - struct_data.get("spot", 0)) / max(struct_data.get("spot", 1), 1) < 0.05
        )
        trade_type = suggest_trade_type(
            iv_rank=vol_data.get("iv_rank", 50),
            term_structure=vol_data.get("term_structure", "flat"),
            capped=capped,
        )

        return {
            "structure_score": structure_score,
            "vol_score": vol_score,
            "flow_score": flow_score,
            "vol_flags": vol_flags,
            "suggested_trade": trade_type,
            "struct_data": struct_data,
            "vol_data": vol_data,
            "flow_data": flow_data,
        }
    except Exception:
        logger.warning("Stage B/C failed for %s", ticker, exc_info=True)
        return None


def run_scan_pipeline(
    cfg: TrendScanConfig,
    *,
    data_fetcher: DataFetcher,
    uw_client: Any = None,
    ib_client: Any = None,
    db_path: str = DEFAULT_DB_PATH,
    json_cache_path: Optional[str] = None,
) -> dict:
    """Run the full 3-stage trend scan pipeline."""
    start = time.monotonic()
    scan_id = _generate_scan_id()
    now = datetime.now(timezone.utc)

    # Build universe
    universe = build_universe(cfg, uw_client=uw_client, ib_client=ib_client)

    # Stage A: parallel TA prefilter
    def _run_stage_a(ticker: str) -> tuple[str, dict | None]:
        return ticker, _stage_a(ticker, data_fetcher, cfg)

    stage_a_raw = parallel_fetch(items=universe, fn=_run_stage_a, max_workers=cfg.max_workers)
    stage_a_results: dict[str, dict] = {t: d for t, d in stage_a_raw if d is not None}

    stage_a_survivors = len(stage_a_results)

    # Stage B+C: structure + vol + flow (on survivors only)
    candidates: list[TrendCandidate] = []
    for ticker, ohlcv in stage_a_results.items():
        bc = _stage_bc(ticker, ohlcv, data_fetcher)
        if bc is None:
            continue

        scores = {
            "trend": ohlcv["trend_score"],
            "structure": bc["structure_score"],
            "volatility": bc["vol_score"],
            "flow": bc["flow_score"],
        }
        final = compute_final_score(scores, cfg.weights)

        flags = list(bc.get("vol_flags", []))

        candidate = TrendCandidate(
            ticker=ticker,
            direction="bullish",
            final_score=final,
            scores=scores,
            spot_price=ohlcv.get("close", 0),
            indicators={
                "ma_20": ohlcv.get("ma_20", 0),
                "ma_50": ohlcv.get("ma_50", 0),
                "ma_200": ohlcv.get("ma_200", 0),
                "rsi": ohlcv.get("rsi", 0),
                "adx": ohlcv.get("adx", 0),
                "macd_histogram": ohlcv.get("macd_histogram", 0),
                "bbw": ohlcv.get("bbw", 0),
                "rs_vs_spy": ohlcv.get("rs_vs_spy", 0),
                "iv_rank": bc["vol_data"].get("iv_rank", 0),
                "gamma_flip": bc["struct_data"].get("gamma_flip", 0),
                "call_wall": bc["struct_data"].get("call_wall", 0),
                "put_wall": bc["struct_data"].get("put_wall", 0),
            },
            summaries={
                "trend": _trend_summary(ohlcv),
                "structure": _structure_summary(bc["struct_data"]),
                "vol": _vol_summary(bc["vol_data"]),
                "flow": _flow_summary(bc["flow_data"]),
            },
            suggested_trade=bc["suggested_trade"],
            invalidation=ohlcv.get("ma_20", 0),
            flags=flags,
        )
        candidates.append(candidate)

    stage_b_survivors = len(candidates)

    # Apply min thresholds and rank
    candidates = apply_min_thresholds(candidates, cfg.min_thresholds)
    ranked = rank_candidates(candidates, top_n=cfg.top_n)

    # Market context
    try:
        market_ctx = data_fetcher.fetch_market_context()
    except Exception:
        market_ctx = {"spy_close": 0, "vix_close": 0, "regime": "unknown"}

    duration = time.monotonic() - start

    # Build output
    output = {
        "scan_id": scan_id,
        "scan_timestamp": now.isoformat(),
        "market_context": market_ctx,
        "universe_size": len(universe),
        "stage_a_survivors": stage_a_survivors,
        "stage_b_survivors": stage_b_survivors,
        "candidates": [
            {
                **c.to_dict(),
                "snapshot_timestamp": now.isoformat(),
            }
            for c in ranked
        ],
    }

    # Write to DuckDB
    try:
        conn = get_connection(db_path)
        init_schema(conn)
        write_scan_run(conn, {
            "scan_id": scan_id,
            "scan_timestamp": now,
            "universe_size": len(universe),
            "stage_a_pass": stage_a_survivors,
            "stage_b_pass": stage_b_survivors,
            "candidates_out": len(ranked),
            "spy_close": market_ctx.get("spy_close", 0),
            "vix_close": market_ctx.get("vix_close", 0),
            "regime": market_ctx.get("regime", "unknown"),
            "duration_secs": duration,
        })
        write_scan_candidates(conn, [
            {
                "scan_id": scan_id,
                "ticker": c.ticker,
                "snapshot_timestamp": now,
                "spot_price": c.spot_price,
                "direction": c.direction,
                "final_score": c.final_score,
                "trend_score": c.scores.get("trend", 0),
                "structure_score": c.scores.get("structure", 0),
                "vol_score": c.scores.get("volatility", 0),
                "flow_score": c.scores.get("flow", 0),
                **{k: v for k, v in c.indicators.items()},
                "suggested_trade": c.suggested_trade,
                "invalidation": c.invalidation,
                "flags": c.flags,
                "holding_window": c.holding_window,
                "trend_summary": c.summaries.get("trend", ""),
                "structure_summary": c.summaries.get("structure", ""),
                "vol_summary": c.summaries.get("vol", ""),
                "flow_summary": c.summaries.get("flow", ""),
            }
            for c in ranked
        ])
        conn.close()
    except Exception:
        logger.warning("Failed to write to DuckDB", exc_info=True)

    # Write JSON cache
    if json_cache_path:
        try:
            write_json_cache(Path(json_cache_path), output)
        except Exception:
            logger.warning("Failed to write JSON cache", exc_info=True)

    return output


# --- Summary generators ---

def _trend_summary(ohlcv: dict) -> str:
    parts = []
    c, m20, m50, m200 = ohlcv.get("close", 0), ohlcv.get("ma_20", 0), ohlcv.get("ma_50", 0), ohlcv.get("ma_200", 0)
    if c > m20 > m50 > m200:
        parts.append("Full MA stack")
    elif c > m20 > m50:
        parts.append("Above 20/50 DMA")
    elif c > m20:
        parts.append("Above 20DMA")
    adx = ohlcv.get("adx", 0)
    if adx:
        parts.append(f"ADX {adx:.0f}")
    rs = ohlcv.get("rs_vs_spy", 0)
    if rs and rs != 1.0:
        parts.append(f"RS {rs:.2f} vs SPY")
    return ", ".join(parts) if parts else "N/A"


def _structure_summary(data: dict) -> str:
    parts = []
    spot = data.get("spot", 0)
    gf = data.get("gamma_flip", 0)
    if spot and gf:
        pct = ((spot - gf) / spot) * 100
        parts.append(f"{'Above' if pct > 0 else 'Below'} gamma flip by {abs(pct):.1f}%")
    cw = data.get("call_wall", 0)
    if spot and cw:
        pct = ((cw - spot) / spot) * 100
        parts.append(f"call wall at +{pct:.0f}%")
    pw = data.get("put_wall", 0)
    if spot and pw:
        pct = ((spot - pw) / spot) * 100
        parts.append(f"put support at -{pct:.0f}%")
    return ", ".join(parts) if parts else "N/A"


def _vol_summary(data: dict) -> str:
    parts = []
    ivr = data.get("iv_rank")
    if ivr is not None:
        parts.append(f"IV rank {ivr:.0f}")
    ts = data.get("term_structure")
    if ts:
        parts.append(f"{ts} term structure")
    ratio = data.get("iv_rv_ratio")
    if ratio:
        parts.append(f"IV/RV {ratio:.2f}")
    return ", ".join(parts) if parts else "N/A"


def _flow_summary(data: dict) -> str:
    parts = []
    cnt = data.get("flow_count", 0)
    if cnt:
        parts.append(f"{cnt} flow prints")
    ask = data.get("ask_dominance", 0)
    if ask:
        parts.append(f"{ask:.0%} ask-side")
    ecr = data.get("expiry_cluster_ratio", 0)
    if ecr >= 0.7:
        parts.append("clustered expiry")
    return ", ".join(parts) if parts else "N/A"


# --- CLI ---

def main() -> None:
    parser = argparse.ArgumentParser(description="Trend scanner")
    parser.add_argument("--top", type=int, default=25, help="Number of top candidates to return")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="DuckDB file path")
    parser.add_argument("--json-cache", default="data/trend_scan.json", help="JSON cache output path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    from scripts.trend_scan_lib.data_fetcher import UWDataFetcher
    from scripts.clients.uw_client import UWClient

    cfg = TrendScanConfig(top_n=args.top)
    uw_client = UWClient()
    data_fetcher = UWDataFetcher(uw_client=uw_client)

    result = run_scan_pipeline(
        cfg,
        data_fetcher=data_fetcher,
        uw_client=uw_client,
        ib_client=None,
        db_path=args.db_path,
        json_cache_path=args.json_cache,
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_scan_e2e.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan.py scripts/tests/test_trend_scan_e2e.py
git commit -m "feat: add trend_scan.py CLI entry point with full 3-stage pipeline"
```

---

### Task 4: Run Full Test Suite

- [ ] **Step 1: Run all trend scanner tests**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_*.py scripts/tests/test_ta_*.py scripts/tests/test_options_*.py scripts/tests/test_volatility.py scripts/tests/test_flow_*.py scripts/tests/test_scanner_lib*.py -v`
Expected: All pass

- [ ] **Step 2: Run uw_scan regression**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_uw_scan*.py -v`
Expected: All pass, no regressions
