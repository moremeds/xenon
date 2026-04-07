# Architecture — High-Throughput & Performance

## High-Throughput Architecture

500+ symbols, <500ms signal-to-order.

**Parallel scanning:** `scanner.py` (15 workers), `discover.py` (10 workers). `--workers N` CLI. `UWRateLimitError` skips ticker, doesn't crash batch.

**Atomic state:** `scripts/utils/atomic_io.py` — `atomic_save()` (temp + `os.replace()` + SHA-256), `verified_load()`. Writers: `ib_sync.py`. Readers: reconcile, flow, free_trade, performance, leap scanner.

**Batched WS relay:** `ib_realtime_server.js` — per-client last-write-wins, 100ms flush. 5000 msg/s → 10 batched/s. Initial state immediate.

**Stale tick detection:** Relay tracks `lastTickTimestamp`, checks every 30s during market hours. No ticks for 45s → auto-restart Gateway (120s cooldown).

**Vectorized math:** `kelly_size_batch()` (NumPy), `portfolio_greeks_vectorized()`. Cross-validated with TS `approxDelta()` to 10⁻¹².

**Resilient IBClient** (`scripts/clients/ib_client.py`): Subscription tracking, disconnect recovery (5 attempts, 2ⁿs capped 30s), pacing violations (162/366: 10s backoff, 3 retries), invalid contracts (200/354: no retry, added to `_failed_contracts`).

**Incremental sync:** `scripts/utils/incremental_sync.py` — diff by `(ticker, expiry)` + contract count, skip full sync when unchanged.

## Performance Page Optimization

`scripts/portfolio_performance.py` — two-phase parallel fetch:
- **Phase A** (sequential): IB stock history + cache checks
- **Phase B** (ThreadPoolExecutor): UW/Yahoo fallbacks + option history. Per-worker `UWClient`.

`PERF_FETCH_WORKERS` env (default 8, clamped 1-20). Disk cache: `data/price_history_cache/`, SHA-256 filenames, TTL 15min/24h. SWR: cached → background rebuild via `POST /performance/background`. Cold start blocks on sync `POST /performance` (180s). Tests: 211 total (160 Python + 51 TS).
