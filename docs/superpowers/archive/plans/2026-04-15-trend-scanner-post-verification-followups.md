# Trend Scanner — Post-Verification Follow-ups

**Context.** The 12-task tribunal-fixes plan (`2026-04-14-trend-scanner-tribunal-fixes.md`) landed in 17 commits on `feat/ta-integration`. A live end-to-end smoke scan run against IB + UW on 2026-04-15 confirmed the scan emits real bullish + bearish candidates with correct structure hints, flags, and invalidation levels. The run also surfaced three real issues that were **not visible in unit / e2e tests**. This file tracks them.

Scan artifact the findings came from: `/tmp/scan_output.json` (scan_id `trend_20260415_0355`, 25 candidates).

---

## 1. `UWClient.get_headlines` is not implemented

**Severity:** Important. Catalyst scoring is permanently stuck at the neutral default (0.5) for every candidate — the 0.10 weight on `scores.catalyst` is effectively dead signal until headlines land.

**Evidence.** Every fresh ticker during the scan logged:

```
WARNING: fetch_catalysts: UW headlines failed for <TICKER>: 'UWClient' object has no attribute 'get_headlines'
```

The Task 10 plan explicitly assumed `uw_client.get_headlines(ticker) -> list[{"type": str, "ts": str}]`. Task 10's `fetch_catalysts` already degrades cleanly via `except Exception` — returns empty catalysts and score 0.5 — so the scan doesn't crash, but the 5-key weight dict (`{trend: 0.30, structure: 0.25, volatility: 0.20, flow: 0.15, catalyst: 0.10}`) is silently 4.5-key.

**Paths forward (pick one):**

1. **Implement `UWClient.get_headlines`.** Likely target endpoint is Unusual Whales `/stock/{ticker}/news` or similar. Normalize their headline payload into the `{"type": str, "ts": ISO8601}` shape that `catalysts.py` already consumes, including mapping UW's vocabulary (e.g. `"rating_change_bullish"`) onto `catalysts.py`'s `_BULLISH_TYPES` / `_BEARISH_TYPES` sets.
2. **Stub as `return []` with a one-time info log**, and mark the catalyst score explicitly as "pending headline source" (different sentinel than 0.5 so it's distinguishable from real neutral).
3. **Drop the catalyst weight to 0** in `TrendScanConfig.weights` until headlines land, and rebalance the other four back to 0.35/0.25/0.20/0.20. Stops the silent dead-weight problem but throws away ranking continuity.

Recommended: option 1 on the next milestone; option 2 as a stopgap within this branch if headlines won't land soon.

**Files touched by any fix:**

- `scripts/clients/uw_client.py` — add `get_headlines(ticker) -> list[dict]`.
- `scripts/trend_scan_lib/stages/catalysts.py` — possibly extend `_BULLISH_TYPES` / `_BEARISH_TYPES` vocabularies to match UW's real type labels.
- `scripts/tests/test_trend_scan_catalysts.py` — add real-UW-response fixture once the shape is known.

---

## 2. `TAService.bulk_refresh` pacing sleep drops the IB socket

**Severity:** Important. Full-universe prep is broken. ~35 tickers refresh, then a 10-minute `_ib_sleep(600)` triggers the IB gateway's idle-socket timeout, the socket closes, the next `reqHistoricalData` raises `ConnectionError: Socket disconnect`, and prep aborts with exit 1. Any universe bigger than the first pacing window cannot be warmed.

**Evidence.** Background prep output:

```
INFO scripts.ta_lib.service: bulk_refresh: pacing — sleeping 10 min before next batch
...
ERROR ib_insync.client: Peer closed connection.
ConnectionError: Socket disconnect
  File "/Users/chenxi/projects/xenon/scripts/ta_lib/service.py", line 263, in bulk_refresh
    self._ib_sleep(600)
```

Counted post-crash: **53 tickers fresh (bar_date=2026-04-14) out of 489 seeded in `data/ta.duckdb`**. Prep left the cache in a partial state — the scan only saw those 53 as Stage A eligible.

**Root causes (both contribute):**

- 10-minute idle window exceeds whatever IB's keepalive policy is on this gateway — the socket is dead before prep wakes up.
- `_ib_sleep` uses `ib_insync.util.run(asyncio.sleep(...))` which does nothing to keep the socket alive.

**Paths forward:**

1. **Reconnect-per-batch.** Before each batch, check `ib_client._ib.isConnected()`; if false, `ib_client.connect(...)` again. Cheap and correct.
2. **Keepalive during sleep.** Periodically issue a no-op (`reqCurrentTime`) every 30-60 seconds during the sleep window instead of one `asyncio.sleep(600)`. Preserves connection state but is more code.
3. **Shorter sleep + smarter pacing.** IB's actual rate limit is known (~60 historical requests per 10 min). If the plan is already under that, a shorter sleep (2-3 min) might avoid the socket-idle issue entirely.

Recommended: option 1 — it's robust to any gateway-side timeout policy and survives restarts.

**Files:**

- `scripts/ta_lib/service.py` — `bulk_refresh` + `_ib_sleep`.
- `scripts/tests/test_ta_lib/test_service.py` — add a test that simulates a disconnected client between batches and verifies reconnect.

---

## 3. `market_context.regime: unknown` when SPY not yet in cache

**Severity:** Minor. The top-level scan output had `{"spy_close": 0, "vix_close": 0, "regime": "unknown"}` because SPY's bars weren't in the first pacing window of prep. `LiveTrendDataFetcher.pre_cache_spy` correctly logged the warning (Task 1 guard is doing its job), but every candidate downstream that would key off regime missed it.

**Paths forward:**

1. **Prioritize SPY + VIX in `bulk_refresh`.** Sort `refresh_tickers` so SPY/VIX/QQQ come first. Cheap insurance against partial refreshes always killing market context.
2. **Let the scanner lazily fetch SPY from Yahoo as a last resort** if both the cache and IB are unavailable. CLAUDE.md's "Never skip to Yahoo/web without trying IB → UW first" allows Yahoo as a last-resort fallback, and market context is informational, not gate-forming.

Recommended: option 1. Five lines in `bulk_refresh`.

**Files:**

- `scripts/ta_lib/service.py` — `bulk_refresh` ticker ordering.

---

## Status

- [ ] Issue 1 — `UWClient.get_headlines`
- [ ] Issue 2 — `bulk_refresh` pacing reconnect
- [ ] Issue 3 — SPY/VIX priority in refresh ordering

None are blocking the tribunal-fixes branch merge. All three affect the **quality** of a production run, not the correctness of the analytical changes the branch landed.
