# Massive.com Historical Data Migration — Design Spec

**Date:** 2026-04-15
**Status:** Design approved (rev 2 post-tribunal review), awaiting plan
**Owner:** Chen Xi
**Related:** `docs/superpowers/specs/2026-04-13-ta-lib-module-design.md`
**Review history:**

- 2026-04-15: Initial draft after brainstorming session
- 2026-04-15: Revised after Codex + Claude tribunal review (Gemini unavailable) — addressed 17 findings: wrong test path, missing callers (`trend_scan.py`, 3 test files), column-name mismatch, timezone normalization, overlap-buffer preservation, lazy `MassiveClient` construction, 1h refresh wiring, 1h freshness contract, partial-write semantics, stale-warning propagation, exit-code pytest collision, `pyproject.toml` marker location, IB-specific helpers removal, FastAPI scheduler verification, CHANGELOG update

---

## 1. Problem

The `ta_lib` module currently fetches historical OHLCV bars from Interactive Brokers via `scripts/ta_lib/bars.py`. IB's historical data endpoint is rate-limited (~60 requests per 10 minutes under the pacing rule), which blocks two workflows:

1. **Cold backfill** — seeding the 2-year TA cache for a full trend-scan universe.
2. **Daily pre-market refresh** — `ta-premarket-prep` hits the limit when the universe grows past a modest size or when a corruption forces a re-seed.

Yahoo Finance is the current cold-start fallback (`ta_seed_yahoo.py`), but its data quality is not trusted. The trend scanner auto-runs at 8:30 AM ET weekdays and needs reliable historical bars at the start of each run.

## 2. Goal

Replace IB as the source of historical OHLCV (anything ≤ yesterday EOD) with **Massive.com**. IB continues to own live quotes, chains, execution, and portfolio. Yahoo Finance is removed from the historical-data path entirely.

## 3. Non-goals

- Replacing IB for live quotes, chains, execution, or portfolio.
- Building a general-purpose vendor-abstraction layer (`DataProvider` interface). Clean cutoff, one source, no dispatch.
- Persisting VWAP or transaction-count columns in `ta.duckdb` (Phase 2).
- Adding minute-bar (`1m`) support (Phase 2).
- Re-designing trend-scan scoring in this spec (separate follow-up).

## 4. Decisions

| Decision          | Choice                                                          |
| ----------------- | --------------------------------------------------------------- |
| Replacement scope | `ta_lib/bars.py` historical fetches — full replacement          |
| Seam model        | Hard EOD cutoff — Massive for ≤ yesterday, IB only for today    |
| Depth             | Seed + daily — Massive owns all historical, not just cold-start |
| Rollout           | Overnight flip, clean cutoff, no parallel validation window     |
| Timeframes        | 1d + 1h                                                         |
| Yahoo             | Remove — `ta_seed_yahoo.py` deleted                             |

## 5. Architecture

### 5.1 New / modified files

**New:**

- `scripts/clients/massive_client.py` — `MassiveClient` (REST, raw `requests`)
- `scripts/tests/test_massive_client.py` — unit tests
- `scripts/tests/test_e2e_massive_pipeline.py` — E2E, `@pytest.mark.e2e` opt-in
- `scripts/ta_reseed_massive.py` — bulk re-seed utility (replaces `ta_seed_yahoo.py`)

**Modified:**

- `scripts/ta_lib/bars.py` — rewritten to call `MassiveClient`. Signature changes:
  old `(ib_client, ticker, duration, bar_size, what_to_show, end_date)`
  → new `(massive_client, ticker, timeframe, lookback_days, overlap_days=5, end_date)`
  — `overlap_days` preserves the current `days_behind + 5` overlap semantics used for corrected-bar refreshes and split detection (see `scripts/ta_lib/service.py:328-360`)
- `scripts/ta_lib/service.py` — multiple changes:
  - `TAService.__init__` drops `ib_client`, accepts optional `massive_client`
  - `TAService.read_only()` classmethod updated (`_ib_client = None` → `_massive_client = None`)
  - Remove `_ib_sleep()` method (lines 265-270) — Massive has no IB-style pacing rule
  - Remove the 10-min inter-batch sleep in `bulk_refresh()` (line 262) — IB-specific pacing
  - `bulk_refresh()` must now refresh **both `1d` and `1h`** timeframes per ticker (currently only refreshes the default `1d`)
  - `_is_stale()` 1h semantics: update so "latest closed session" is valid mid-session (see §5.4 resolution)
- `scripts/ta_premarket_prep.py`:
  - **Lazy** `MassiveClient` construction — created only inside the refresh path, never in `--audit-only` (which must remain network-free)
  - Applies 50% failure-ratio abort threshold (exit code **3**; code 2 collides with pytest's collection-error convention)
  - Explicitly classifies + refreshes **both `1d` and `1h`** (currently only `1d` is audited)
  - Writes `data/ta_premarket_status.json` metadata on each run (`last_run_ts`, `status`, `failed_ratio`, `failure_reason`) so `trend_scan` can surface staleness (see §6)
- `scripts/ta_cli.py`:
  - **Lazy** `MassiveClient` construction — `--stats`, `--query`, `--cache-only` must work without `MASSIVE_API_KEY`
  - `--refresh` loops both timeframes (current behavior preserved)
- `scripts/trend_scan.py` — line 803 `TAService(db_path=..., ib_client=ib_client)` → `TAService(db_path=..., massive_client=<lazy>)`; also reads `ta_premarket_status.json` and surfaces any prior-run failure in scan output
- `scripts/tests/test_ta_lib/test_service.py` — drop `ib_client=mock_ib` constructor args; substitute `MassiveClient` mock
- `scripts/tests/test_ta_lib/test_snapshot_contract.py` — same constructor change
- `scripts/tests/test_ta_lib/test_premarket_prep.py` — update fake TAService + add regression tests for `--audit-only` with `MASSIVE_API_KEY` unset, and for both-timeframe refresh
- `scripts/tests/test_ta_lib/test_bars.py` — rewritten for new signature + Massive mock
- `scripts/api/server.py` — **no code change**, but verify: the scheduler's `run_script("ta_premarket_prep.py", ...)` exit-code handling (`result.ok`) still maps correctly after exit code 2 → 3 change. Add assertion in the cutover runbook.
- `pyproject.toml` — add `e2e: live tests hitting real Massive API (requires MASSIVE_API_KEY)` to `[tool.pytest.ini_options] markers` list (existing `integration` marker stays)
- Root `CLAUDE.md` — Data Source Priority amended; `MASSIVE_API_KEY` added to credentials table
- `scripts/CLAUDE.md` — `ta_lib` section reflects Massive-backed bars; command table updated
- `CHANGELOG.md` — new entry under an appropriate version bump (historical-data vendor swap is a notable migration)
- `.env` (root) — `MASSIVE_API_KEY` added (not committed)

**Deleted:**

- `scripts/ta_seed_yahoo.py`
- `scripts/tests/test_ta_lib/test_seed_yahoo.py` — actual location (spec previously had wrong path)

### 5.2 `MassiveClient` public surface

```python
class MassiveClient:
    def __init__(self, api_key: str | None = None, timeout: float = 10.0) -> None:
        """Load MASSIVE_API_KEY from env if not passed. Fail fast if missing."""

    def get_aggregates(
        self,
        ticker: str,
        timeframe: str,          # "1d" | "1h"
        from_date: str,          # YYYY-MM-DD
        to_date: str,            # YYYY-MM-DD
        adjusted: bool = True,
    ) -> pd.DataFrame:
        """Return [date, open, high, low, close, volume, vwap, tx_count]
        ordered ascending by date."""

    def health_check(self) -> bool:
        """Cheap endpoint to verify auth + connectivity."""
```

Timeframe mapping:

```python
_TIMEFRAME_MAP = {
    "1d": (1, "day"),
    "1h": (1, "hour"),
}
```

Endpoint:

```
GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
    ?adjusted=true&sort=asc&limit=50000
Authorization: Bearer ${MASSIVE_API_KEY}
```

Exception hierarchy:

```python
class MassiveError(Exception): ...
class MassiveAuthError(MassiveError): ...      # no retry
class MassiveNoDataError(MassiveError): ...    # no retry, skip ticker
class MassiveRateLimitError(MassiveError): ... # retry with backoff
```

Retries: exponential backoff (1s, 2s, 4s with jitter) on 429 / 5xx / network, max 3 attempts.

### 5.3 Data flow

```
.env (MASSIVE_API_KEY)
        │
        ▼
ta_premarket_prep (6 AM ET cron)
        │
        ▼
MassiveClient ──────► Massive REST (/v2/aggs/…)
        │
        ▼ OHLCV (VWAP + tx_count dropped at ta_lib seam)
ta.duckdb (store.py — unchanged schema)
        ▲
        │ read-through
trend_scan Stage A (8:30 AM ET cron)
```

- `MassiveClient` returns 8 columns (incl. VWAP + tx_count).
- `ta_lib/bars.py::fetch_bars()` selects `[date, open, high, low, close, volume]` (full names matching `store.write_ohlc`'s contract — see `scripts/ta_lib/store.py:79-104`) before handing to `store.write_ohlc`. VWAP + tx_count are dropped here.
- `ta.duckdb` schema **unchanged** for v1.
- IB is not called in the historical code path. `TAService.__init__` no longer accepts `ib_client`.

**Timestamp / timezone convention (1h bars):** Massive returns unix ms timestamps (UTC). `store.py` stores `bar_date` as `TIMESTAMPTZ`. Before write, `fetch_bars` converts Massive timestamps to **`America/New_York` (ET)** and writes as tz-aware `TIMESTAMPTZ`. This matches `_is_stale()`'s ET-aware session comparison (`service.py:295-314`) and survives DST transitions. Unit test coverage required for spring-forward and fall-back boundaries.

### 5.4 Today's-bar and intraday-freshness handling

Trend scan runs pre-market at 8:30 AM ET — no "today's bar" yet. Stage A consumes only closed bars.

**Contract change for `1h` during market hours.** Current `_is_stale()` (`service.py:295-314`) treats 1h data as stale if more than ~2 hours behind wall-clock ET during RTH. That contract assumed IB could backfill "today so far" on demand. With the clean cutoff, there is no vendor that fills today's in-progress hours.

Resolution for v1 (the simple path that fits "Massive ≤ yesterday, IB for live"):

- `_is_stale()` for `1h`: during RTH, stale means "latest bar older than yesterday's last closed RTH hour (15:30 ET on the prior trading day)". Today's in-progress 1h bars are **not** fetched by `fetch_bars`.
- Intraday consumers that need current price → `IBClient.get_snapshot()`, not `TAService`.
- `ta-cli` during RTH reads whatever is in DuckDB and reports the `latest_ts` so the operator sees it.
- Post-RTH (after 16:00 ET), the day's closed 1h bars become available from Massive on the next `ta_premarket_prep` run (next morning). Filling today's closed hours same-day is a Phase-2 enhancement.

Revised `_is_stale()` plus tests for the new RTH/post-RTH boundary are part of this migration.

### 5.5 Thread safety

`MassiveClient` holds a single `requests.Session` (thread-safe for reads). Shared across scanner thread pool via `scanner_lib/executor.py::parallel_fetch`.

## 6. Error handling

| Failure                               | Behavior                                                                                                                                                                                                                                                                                |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Missing `MASSIVE_API_KEY`             | `MassiveAuthError` at `__init__` — fail fast at import time                                                                                                                                                                                                                             |
| 401 / 403                             | `MassiveAuthError`, no retry, abort run, exit code 1                                                                                                                                                                                                                                    |
| 404 / empty results                   | `MassiveNoDataError`, skip ticker, INFO log                                                                                                                                                                                                                                             |
| 429                                   | Retry 3× with backoff; then `MassiveRateLimitError`, skip ticker, WARN log                                                                                                                                                                                                              |
| 5xx / network                         | Same retry policy as 429                                                                                                                                                                                                                                                                |
| Per-ticker sanity (neg vol, NaN OHLC) | Drop row, WARN log; never persist invalid OHLC                                                                                                                                                                                                                                          |
| Partial data (<80% expected bars)     | Persist + WARN log                                                                                                                                                                                                                                                                      |
| **>50% of universe fails**            | Abort `ta_premarket_prep` with exit code **3**; write `ta_premarket_status.json{status:"aborted", failure_reason:"vendor_down"}`; previously-written per-ticker rows in DuckDB remain (see "Partial-write semantics" below); 8:30 scan reads status file and surfaces warning in output |

**No automatic IB fallback** — by design. Scanner accepts staleness with a visible warning rather than silently swapping sources.

**Partial-write semantics.** `bulk_refresh` commits per ticker (`service.py:317-389`). The 50% abort threshold is checked _during_ the loop — tickers already refreshed stay. This is intentional: a partial refresh is strictly more recent than the prior day's cache, so no rollback is warranted. The status file distinguishes "full success" from "partial/aborted" so the scanner can warn on staleness even when some bars are today's.

**Stale-warning propagation.** `trend_scan.py` at startup reads `data/ta_premarket_status.json`:

- missing or `status=ok` → no warning
- `status=aborted` with `failure_reason` → WARN log + prepend warning block to `data/trend_scan.json` output
- stale file (age > 1 trading day) → WARN log, continue

This replaces the current heuristic at `trend_scan.py:796-803` that only warns on IB unavailability.

## 7. Cutover plan

### 7.1 Pre-cutover

- Provision Massive account, obtain API key
- Add `MASSIVE_API_KEY` to root `.env`
- Document in root `CLAUDE.md` credentials table
- Smoke-test one ticker via shell + `curl`

### 7.2 Implementation (one PR, staged commits — merged atomically)

1. `MassiveClient` + unit tests + `.env` key (no behavior change)
2. Rewrite `ta_lib/bars.py` to use `MassiveClient` with new signature + overlap preservation + ET timezone conversion (breaks all existing callers)
3. Update `TAService` — drop `_ib_client`, `_ib_sleep()`, pacing logic; add `massive_client`; refresh both timeframes; update `_is_stale()` 1h semantics (compiles again)
4. Update all TAService callers: `trend_scan.py:803`, `ta_premarket_prep.py`, `ta_cli.py` (lazy MassiveClient); exit code 2 → 3; status-file writer
5. Update test suites: `test_service.py`, `test_snapshot_contract.py`, `test_premarket_prep.py`, `test_bars.py` — remove `ib_client=`, add Massive mocks, add regression for `--audit-only` offline and for both-timeframe refresh
6. Delete `ta_seed_yahoo.py` + `scripts/tests/test_ta_lib/test_seed_yahoo.py`
7. Add `scripts/ta_reseed_massive.py` operator script (loops both timeframes)
8. Update `pyproject.toml` — add `e2e` marker
9. Update root `CLAUDE.md`, `scripts/CLAUDE.md`, `CHANGELOG.md`
10. Verify `scripts/api/server.py` scheduler paths still work with exit code 3 (no code change, smoke test)

Intermediate commits may be red — only the merged state is green. Bisect-friendly post-merge.

### 7.3 Cutover-day runbook (weekend preferred)

```bash
TZ=America/New_York date +"%A %H:%M"

python3.13 -c "from scripts.clients.massive_client import MassiveClient; \
    print(MassiveClient().health_check())"

cp data/ta.duckdb data/ta.duckdb.pre-massive.bak

python3.13 scripts/ta_reseed_massive.py --timeframes 1d,1h --lookback-days 730

python3.13 scripts/ta_cli.py --stats
# Expect ~500 bars/ticker for 1d, ~3500/ticker for 1h

python3.13 scripts/ta_cli.py AAPL TSLA SPY --history | tail -20
# Manual spot-check vs IB TWS

python3.13 scripts/ta_premarket_prep.py --audit-only
python3.13 scripts/trend_scan.py
```

Timing: not within 2 hours of 6 AM ET or 8:30 AM ET. Recommended: Saturday midday or Sunday.

### 7.4 Rollback

Within 7 days post-cutover:

```bash
cp data/ta.duckdb.pre-massive.bak data/ta.duckdb
git revert <cutover-merge-commit>
```

After 7 days, backup is stale enough that an IB backfill would still hit the original rate-limit pain — backup deleted, rollback requires a forward-fix instead.

## 8. Testing

### 8.1 Unit tests (mocked HTTP, CI-safe)

- `test_massive_client.py` — happy path, timeframe mapping, `adjusted=False`, 429×3 → `MassiveRateLimitError`, 401 → immediate `MassiveAuthError`, empty → `MassiveNoDataError`, `health_check`, missing key at `__init__`
- `test_ta_lib/test_bars.py` — VWAP/tx_count stripped at store seam, malformed rows filtered, new signature

### 8.2 Integration tests (mocked Massive, real DuckDB)

- `TAService.bulk_refresh` — 2 tickers × 2 timeframes end-to-end, indicators populate
- Pre-market prep 60% failure injection → exit code **3**, status file written with `status=aborted`
- `ta_premarket_prep --audit-only` runs with `MASSIVE_API_KEY` unset — no network, no exception (lazy construction regression test)
- `ta_cli --stats`, `--query`, `--cache-only` run with `MASSIVE_API_KEY` unset — no network, no exception
- `_is_stale()` — 1h bars at 11:00 AM ET with latest = 15:30 prior trading day → **not stale** (new contract); 1h with latest = 3 trading days ago → **stale**
- `fetch_bars` overlap — `latest_cached=yesterday`, `overlap_days=5` → fetches last ~7 calendar days, upserts
- `trend_scan` reads `ta_premarket_status.json` with `status=aborted` → WARN log + warning block in output
- DST boundary: 1h bar on spring-forward / fall-back day → correct ET timestamps in DuckDB

### 8.3 E2E tests (live Massive, opt-in, `@pytest.mark.e2e`)

Gated on `MASSIVE_API_KEY` presence — auto-skip if unset.

- **E2E-1 Smoke:** `MassiveClient.get_aggregates("AAPL", "1d", 30d window)` → DuckDB round trip, ≥ 18 rows, monotonic dates, OHLC > 0, no NaN.
- **E2E-2 Full pipeline:** 5-ticker mini-universe, `ta_reseed_massive.py --lookback-days 90`, then `trend_scan.py --dry-run`. Assert Stage A emits ≥ 1 candidate with non-null indicators.
- **E2E-3 Vendor-disagreement gate:** For AAPL/SPY/TSLA, compare last 5 closed daily bars from IB vs Massive. Assert close-price agreement within 0.5% per bar. **Must pass before merging.** Post-merge, demote to manual.

### 8.4 Coverage

95% target against unit + integration. E2E is assurance, not coverage credit.

### 8.5 Running

Markers live in `pyproject.toml → [tool.pytest.ini_options] → markers`. Add an `e2e` entry alongside the existing `integration` marker.

```bash
python3.13 scripts/run_pytest_affected.py           # unit + integration
python3.13 -m pytest -m e2e -xvs                    # E2E only
python3.13 -m pytest -m "not e2e" -xvs              # default (no live vendor)
```

## 9. Policy changes

Root `CLAUDE.md` Data Source Priority block:

```
1. Interactive Brokers — real-time quotes, chains, execution, live portfolio
2. Massive.com — historical OHLCV (≤ yesterday EOD), 1d + 1h
3. Unusual Whales ($UW_TOKEN) — dark pool, sweeps, alerts
4. Web scrape — last resort

Never use Yahoo Finance. Historical data is sourced exclusively from Massive.
```

Root `CLAUDE.md` credentials table: add `MASSIVE_API_KEY` row under root `.env`.

`scripts/CLAUDE.md`: `ta_lib` section documents Massive-backed bars; command table replaces `ta-seed-yahoo` with `ta-reseed-massive`.

## 10. Risks & open questions

| #   | Risk                                                                          | Mitigation                                                                                                                              |
| --- | ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Adjustment divergence between Massive and IB (splits/dividends)               | E2E-3 close-price agreement gate pre-merge                                                                                              |
| 2   | Massive outage during pre-market                                              | 50% failure-ratio abort + staleness warning in scanner; operator re-seeds on recovery                                                   |
| 3   | Rate limit in practice ≠ "unlimited" marketing                                | Exponential backoff; logs surface `429` counts; revisit if retries spike                                                                |
| 4   | Dividend-adjustment convention not explicitly documented                      | Confirm against high-yield ticker during cutover smoke test                                                                             |
| 5   | History depth not explicitly documented per endpoint                          | Verify 2-year coverage on 1h during E2E-1                                                                                               |
| 6   | Vendor lock-in for historical data                                            | Accepted — clean cutoff is the tradeoff for simplicity                                                                                  |
| 7   | Corporate actions between cutover and any future rollback                     | 7-day backup window bounds the risk                                                                                                     |
| 8   | Intraday `1h` freshness gap — today's in-progress hours not fetched by anyone | Accepted for v1: `_is_stale()` contract narrowed to "yesterday's last closed RTH hour"; same-day fill is Phase-2                        |
| 9   | Partial refresh after mid-run abort leaves mixed-vintage cache                | Status file distinguishes partial from clean; scanner surfaces warning; operator can re-run `ta_reseed_massive.py` to force consistency |
| 10  | Massive timestamp convention divergence (UTC vs session-local)                | Canonical ET conversion at write time + DST regression tests (§8.2)                                                                     |

## 11. Follow-ups (Phase 2)

1. **Screener review** — re-examine `trend_scan` Stage A scoring + thresholds after the data source switch. Verify ranking is still well-calibrated against Massive's numbers. Requested by owner during brainstorming.
2. **VWAP + tx count persistence** — extend `ta.duckdb` schema, surface in `trend_scan_lib/stages/ta_prefilter.py`. Unlocks volume-quality and execution-quality signals.
3. **`MassiveApiStats` observability** — singleton mirroring `uw_api_stats.py`, FastAPI `/massive-stats`, optional sidebar widget.
4. **Minute-bar support + pagination** — add `1m` to timeframe map, implement `limit` + `next_url` pagination in `MassiveClient`.
5. **Broader Massive adoption** — options historicals, index data, potentially replacing UW for some ranges.
6. **Alerting** — once a Slack/email alerting framework exists, hook pre-market prep exit code 3 into it.
7. **Same-day `1h` intraday fill** — close the gap introduced by v1's "yesterday's last closed RTH hour" contract. Would require either a live minute-aggregate stream from Massive (WebSocket Phase 2) or an IB-based intraday filler.

## 12. Success criteria

- `trend_scan.py` runs successfully at 8:30 AM ET on first post-cutover weekday without IB historical calls.
- `ta_premarket_prep.py` completes within its current time budget and refreshes **both** `1d` and `1h` timeframes.
- E2E-3 passes (IB vs Massive close-price divergence < 0.5% on 3 liquid tickers).
- `ta_premarket_prep --audit-only` succeeds with `MASSIVE_API_KEY` unset (no network).
- `ta_cli --stats` / `--query` / `--cache-only` succeed with `MASSIVE_API_KEY` unset.
- Unit + integration coverage ≥ 95% on new/modified files.
- `grep -r "ta_seed_yahoo\|yfinance" scripts/ docs/` returns zero hits in non-historical paths (allowed: changelog, archived docs).
- DST regression tests (spring-forward, fall-back) pass for 1h bars.
- `trend_scan` correctly surfaces a warning when `ta_premarket_status.json` indicates `status=aborted`.
