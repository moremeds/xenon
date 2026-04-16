# Apex R2 ETL — Historical OHLCV + TA in a Nightly GitHub Action

**Status:** Approved for planning
**Date:** 2026-04-16
**Branch:** `feat/apex-r2-etl` (off `master`)
**Supersedes:** `2026-04-15-massive-historical-data-migration-design.md` (scanner-local Massive fetch)

---

## 1. Goal

Stop the trend scanner from calling Massive.com at scan time. Produce OHLCV and pre-computed TA indicators once per night in a GitHub Action, publish as Parquet on Cloudflare R2 (`apex-data` bucket), and have the scanner act as a pure consumer: download, join, classify.

**Success criteria:**

- `scripts/trend_scan.py` never imports `MassiveClient` or makes any HTTP call to Massive.
- Action runs nightly around 00:00 UTC Tue–Sat (= 7–8 PM ET Mon–Fri evenings depending on DST; incremental) and 05:00 UTC Sat (= 00:00–01:00 ET Sat; full refresh). Completes under 60 min.
- Scanner cold-start (sync + run) completes under 45 seconds from stale mirror.
- R2 `meta/last_updated.json` reflects our writes; external producer's `meta/universe.json` stays authoritative and untouched.

## 2. Architecture

```
External producer (unchanged) ──▶ meta/universe.json, meta/market_caps.json,
                                   meta/float_shares_cache.json
                                             │ read
                                             ▼
                      ┌──────────────────────────────────────┐
                      │  GitHub Action (apex-data-refresh)   │
    ┌─────────────┐   │                                      │
    │ Massive.com │◀──┤  1. read meta/universe.json from R2  │
    └─────────────┘   │  2. fetch OHLCV (incremental M–F,    │
                      │     full Saturday)                   │
                      │  3. compute TA indicators            │
                      │  4. write Parquet + manifest         │
                      └─────────────┬────────────────────────┘
                                    │ S3 PUT
                                    ▼
                      ┌──────────────────────────────────────┐
                      │  R2 apex-data (shared)               │
                      │                                      │
                      │  parquet/historical/{1d,1h}/{T}.parq ← we write
                      │  parquet/indicators/{1d,1h}/{T}.parq ← we write (NEW)
                      │  meta/last_updated.json              ← we update historical+indicators
                      │  meta/data_quality.json              ← we update
                      │  meta/universe.json                  ← read-only for us
                      │  meta/market_caps.json               ← read-only for us
                      │  signals/                            ← untouched
                      └─────────────┬────────────────────────┘
                                    │ S3 GET
                                    ▼
                      ┌──────────────────────────────────────┐
                      │  Local scanner (trend_scan)          │
                      │                                      │
                      │  1. apex_sync: R2 → data/apex_mirror │
                      │     (gated by meta/last_updated.json)│
                      │  2. load parquets via pyarrow;       │
                      │     join universe (pandas)           │
                      │  3. Stage A gate (price/mcap/$vol)   │
                      │  4. Stage B/C: UW structure/vol/flow │
                      │  5. rank, write data/trend_scan.json │
                      └──────────────────────────────────────┘
```

**Core invariant:** vendor calls, TA computation, and failure recovery live in the Action (unlimited wall-clock). Scanner's inputs are R2 objects + its own logic. No network calls to Massive from scanner.

## 3. R2 bucket contract

### Ownership

| Prefix / Key                                            | Owner                | Access                                |
| ------------------------------------------------------- | -------------------- | ------------------------------------- |
| `parquet/historical/{1d,1h}/{TICKER}.parquet`           | Our Action           | R/W                                   |
| `parquet/indicators/{1d,1h}/{TICKER}.parquet` (NEW)     | Our Action           | R/W                                   |
| `meta/last_updated.json`                                | Our Action           | R/W (conditional PUT with `If-Match`) |
| `meta/data_quality.json`                                | Our Action           | R/W                                   |
| `meta/universe.json`                                    | External producer    | Read-only for us                      |
| `meta/market_caps.json`, `meta/float_shares_cache.json` | External producer    | Read-only for us                      |
| `signals/*`                                             | Third-party pipeline | Untouched                             |
| `parquet/historical/{4h,30m,1w}/*`                      | External producer    | Untouched (we don't write these)      |

### Parquet schemas

```
parquet/historical/{tf}/{T}.parquet (was written by external producer with a tz bug; we re-establish the schema)
  timestamp   timestamp[us, tz=UTC]   # see §3.1 "Timestamp normalization"
  open        float64
  high        float64
  low         float64
  close       float64
  volume      int64
  # NOTE: existing producer files carry an extra `__index_level_0__` column
  # (pandas reset_index residue). We DROP it on read and never emit it on write.

parquet/indicators/{tf}/{T}.parquet (NEW)
  timestamp            timestamp[us, tz=UTC]
  sma_20               float64
  sma_50               float64
  sma_200              float64
  rsi_14               float64
  adx_14               float64
  macd                 float64
  macd_signal          float64
  macd_histogram       float64
  bb_upper             float64
  bb_middle            float64
  bb_lower             float64
  bb_width             float64
  atr_14               float64
  high_20d             float64
  low_20d              float64
  high_52w             float64
  low_52w              float64
  up_day_volume_ratio  float64
```

Rows match 1-to-1 with the ticker's historical parquet (same timestamps). Computed at Action time, never recomputed by scanner.

**Resolution note:** we emit microsecond (`us`) precision, not nanosecond (`ns`) like the existing producer. Microsecond is ample for bar-level data (daily + hourly; we never handle sub-second), reads well in pandas (pandas 2.x default is `ns` but auto-converts), and is slightly smaller on disk. DuckDB and pyarrow both handle both natively.

### 3.1 Timestamp normalization — the Asia/Hong_Kong bug

**Finding (verified against live R2 data 2026-04-16):** existing `parquet/historical/{1d,1h}/*.parquet` files carry Arrow schema `timestamp[ns, tz=Asia/Hong_Kong]`. Inspection of hourly values (`2025-11-28 09:30:00+0800`) shows the producer wrote ET-local wall-clock times but labeled them HKT — almost certainly a machine-local tz leaking into an unguarded `tz_localize()`. Daily bars are less broken (only the date matters) but still misleading.

**Our policy — applies to every file we write:**

1. **Writer** (`parquet_store.write_ohlcv`, `parquet_store.write_indicators`) always emits `timestamp[us, tz=UTC]`. For daily bars, the UTC representation of the trading day is `YYYY-MM-DD 00:00:00+00:00`. For hourly bars, it's the true UTC hour of the bar open.
2. **Incremental-mode reader** (`parquet_store.read_ohlcv`) detects the old schema (`tz != UTC`) and normalizes **by re-interpreting the wall-clock as `America/New_York`** (not HKT — the values are ET numbers), then converting to UTC. Concretely:

   ```python
   if ts.dt.tz is not None and str(ts.dt.tz) != "UTC":
       # strip the wrong tz label, re-localize as ET, convert to UTC
       ts = ts.dt.tz_localize(None).dt.tz_localize("America/New_York").dt.tz_convert("UTC")
   ```

3. **On first write after migration** (weeknight incremental): the file gets a new schema tag `timestamp[us, tz=UTC]` because we rewrite the whole parquet (not an Arrow-level append). Subsequent reads see UTC and skip the normalization branch.
4. **Full-refresh Saturday**: wholly rewrites from Massive → UTC-native, sidestepping the normalization path.

**Caveat:** if anyone else reads these files (e.g. the `signals/` pipeline), our rewrite changes the stored tz from `Asia/Hong_Kong` to `UTC`. For daily bars the date unchanged; for hourly bars the wall-clock values _shift by 13 hours_ (the correction is exactly what they should be — but a consumer hardcoded to HKT would read wrong). This needs coordination with whoever runs `signals/` before rollout step 4 (manual first full refresh). Logged in §7 Risks.

### Manifest (`meta/last_updated.json`)

```json
{
  "universe": "2026-04-16T08:27:29.613039+00:00", // external owner
  "historical": "2026-04-16T20:05:12.000000-04:00", // our Action
  "indicators": "2026-04-16T20:08:44.000000-04:00", // our Action (NEW key)
  "schema_version": 1 // NEW — gates scanner compatibility
}
```

Manifest is read-modify-write with conditional PUT (`If-Match: <etag>`). Retry on mismatch with exponential backoff. `schema_version` increments when parquet schemas change; scanner refuses unknown versions.

### Data quality (`meta/data_quality.json`)

After each Action run, append per-ticker rows recording `{symbol, timeframe, bars, first_bar, last_bar, coverage_pct, gaps[], status, run_id}`. Keep rolling 30-day window. Operator can `jq '.entries[] | select(.status == "FAIL")'` to diagnose.

## 4. Components

### New files

- **`.github/workflows/apex-data-refresh.yml`**
  - Triggers: cron `0 0 * * 2-6` UTC (fires Tue–Sat at 00:00 UTC, which is Mon–Fri 7–8 PM ET after market close, incremental mode), cron `0 5 * * 6` UTC (Saturday 05:00 UTC ≈ early Saturday ET, full mode), `workflow_dispatch` (manual).
  - Secrets: `MASSIVE_API_KEY`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT`, `R2_BUCKET`.
  - Runtime: Ubuntu 24.04, Python 3.13, ta-lib via apt, boto3 + pyarrow + pandas.
  - Timeout: 90 min.

- **`scripts/apex_refresh.py`** — Action entrypoint. Flags: `--mode {incremental,full}`, `--timeframes 1d,1h`, `--dry-run`, `--universe-override PATH`.

- **`scripts/ta_lib/r2_store.py`** — sole owner of `boto3.client("s3")` calls. Public surface:

  ```python
  def get_object(key: str) -> bytes
  def put_object(key: str, body: bytes, if_match: str | None = None) -> str  # returns etag
  def list_objects(prefix: str) -> Iterator[tuple[str, int, datetime]]
  def get_json(key: str) -> dict
  def put_json(key: str, data: dict, if_match: str | None = None) -> str
  def head(key: str) -> dict | None  # returns None on 404
  ```

  Retry policy: 3 attempts, exponential backoff on 5xx and connection errors. 4xx surfaces as `R2ClientError`.

- **`scripts/ta_lib/parquet_store.py`** — pyarrow read/write. Schemas declared as constants; writes enforce UTC tz; reads validate schema on load (reject schema mismatches loudly). Supports dedupe-on-append (incremental mode).

- **`scripts/ta_lib/apex_sync.py`** — scanner-side sync:
  ```python
  def sync_if_stale(
      mirror_dir: Path = Path("data/apex_mirror"),
      timeframes: tuple[str, ...] = ("1d", "1h"),
      *,
      force: bool = False,
  ) -> SyncResult
  ```
  Compares `meta/last_updated.json` `historical` + `indicators` timestamps vs `mirror_dir/.last_sync.json`. Parallel downloads (10 workers) using `r2_store.list_objects`. Atomic: writes to `mirror_dir.tmp/` then `rename`. Emits `SyncResult(synced: bool, stale_reason: str | None, files_downloaded: int, errors: list[str])`.

### Modified files

- **`scripts/ta_lib/service.py` (`TAService`)** — drops `massive_client` constructor arg, drops `bulk_refresh`, drops `_is_stale`. New constructor: `TAService(mirror_dir="data/apex_mirror")`. Public API (`get_snapshot`, `get_indicators`) unchanged; stage code paths untouched. Reads via pyarrow; in-memory DuckDB only used if we need SQL joins (deferred; start with pandas).

- **`scripts/ta_lib/bars.py`** — shrinks to the Massive→DataFrame adapter only, called exclusively by `apex_refresh.py`. TAService no longer imports it.

- **`scripts/trend_scan.py`** — startup:
  1. `apex_sync.sync_if_stale()` (replaces `ta_premarket_status` check)
  2. `universe_rows = load_universe_from_mirror()` (replaces `build_universe`)
  3. Stage A joins `universe_rows` into OHLCV for the new floor filters
  4. Stage B/C unchanged

- **`scripts/trend_scan_lib/config.py`** — add `min_market_cap` (already present at 1B), `min_turnover_rate`, `exclude_tiers: set[str]`.

- **`scripts/trend_scan_lib/universe.py`** — simplified to `load_universe_from_mirror(mirror_dir: Path) -> list[dict]`. No SP500/NDX100/UW/watchlist logic. Returns the `tickers` array verbatim from `meta/universe.json`.

### Deleted files

- `scripts/ta_premarket_prep.py`
- `scripts/api_status/ta_premarket_status.py` + tests
- `scripts/ta_reseed_massive.py`
- `data/ta_premarket_universe.json`, `data/ta_premarket_status.json` (runtime artifacts)
- `data/ta.duckdb` (runtime; parquet mirror supersedes)
- FastAPI scheduler hook for `ta_premarket_prep` in `scripts/api/server.py`

## 5. Data flow

### Nightly Action (producer)

```
1. Checkout repo, install Python 3.13, ta-lib (apt), boto3, pyarrow, pandas
2. Load secrets from GH → env
3. r2.get_json("meta/universe.json") → 2,308 tickers with per-ticker timeframes[]
4. Mode = incremental (weeknight) or full (Saturday)
5. ThreadPoolExecutor(max_workers=10):
   for (ticker, tf) in expand_universe(universe, timeframes=["1d","1h"]):
       existing = r2.get_parquet(f"parquet/historical/{tf}/{ticker}.parquet") if incremental else None
       last_ts = max(existing.timestamp) if existing else None
       new_bars = massive.fetch(ticker, tf, start=last_ts or "2y_ago", end=yesterday_et())
       df = pd.concat([existing, new_bars]).drop_duplicates("timestamp").sort_values("timestamp")
       indicators = compute_indicators(df)
       r2.put_parquet(f"parquet/historical/{tf}/{ticker}.parquet", df)
       r2.put_parquet(f"parquet/indicators/{tf}/{ticker}.parquet", indicators)
6. r2.put_json("meta/last_updated.json", {..., "historical": now_iso(), "indicators": now_iso(),
                                          "schema_version": 1}, if_match=prev_etag)
7. r2.put_json("meta/data_quality.json", accumulated_dq)
8. Exit 0 on success; nonzero on any unrecoverable failure (propagates to GH action status)
```

**Incremental footprint:** typical weeknight = 1 bar per (ticker, tf) = ~5 KB Massive response × 4,616 fetches ≈ trivial. Full refresh: ~500 × 2,308 × 2 ≈ 30 min end-to-end.

### Scan-time (consumer)

```
trend_scan.py main():
  1. apex_sync.sync_if_stale()
     - GET meta/last_updated.json (1 KB)
     - compare R2.historical vs local .last_sync.json.historical
     - if stale: parallel GET ~4,616 files (~150 MB, ~3–5s)
     - write .last_sync.json
  2. universe_rows = load_universe_from_mirror()   # list[dict], 2,308 rows
  3. ta_service = TAService(mirror_dir="data/apex_mirror")
  4. Stage A:
     - for each ticker in universe_rows:
         ohlcv = ta_service.get_snapshot(ticker)   # reads from local parquet
         if ohlcv is None: skip (missing data)
         apply Stage A hard filters (price, $vol, mcap, tier)
         apply trend gate (bull: close > ma_20, rsi > 50; bear: mirror)
         score by trend
     - cap top 100 by trend_score per direction
  5. Stage B/C (UW): structure + vol + flow for survivors (unchanged)
  6. Rank, write data/trend_scan.json
```

### Failure matrix

| Failure                                             | Behavior                                                                                                                                                                                |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Action fails mid-run (e.g., Massive 5xx storm)      | Partial parquets written; manifest NOT updated (step 6 guarded by overall try/except). Next run starts from same baseline. Scanner sees stale `last_updated` → logs warning, continues. |
| Single ticker fetch fails in Action                 | Logged to `data_quality.json` with reason; ticker's parquet untouched; other tickers continue.                                                                                          |
| Manifest `If-Match` mismatch (two overlapping runs) | Retry 3x with fresh ETag; on final failure, log and exit nonzero.                                                                                                                       |
| R2 unreachable during scan                          | `sync_if_stale` catches; scanner falls back to local mirror with warning in `trend_scan.json["warnings"]`.                                                                              |
| Local mirror missing + R2 unreachable               | Scanner aborts with clear error (no silent fallback).                                                                                                                                   |
| Schema version mismatch                             | Scanner aborts with clear error naming expected vs found version.                                                                                                                       |

## 6. Stage A gate — hard filters via universe.json join

With `meta/universe.json` carrying `marketCap`, `dollar_volume`, `turnover_rate`, `tier` inline, Stage A floors become a pure join. No OHLCV inspection needed for the floors; no vendor calls.

```python
# scripts/trend_scan_lib/config.py — floors (all configurable, survive the rewrite)
min_price: float = 5.0
min_dollar_volume: float = 10_000_000
min_market_cap: float = 1_000_000_000
min_turnover_rate: float = 0.0
exclude_tiers: set[str] = set()   # e.g., {"leveraged_etf", "inverse_etf"} if desired
```

```python
# scripts/trend_scan.py
def _stage_a_data(ticker: str, universe_row: dict, data_fetcher, cfg) -> dict | None:
    ohlcv = data_fetcher.fetch_ohlcv(ticker)   # reads local parquet — cannot fail on rate limit
    if ohlcv is None:
        return None
    if ohlcv.get("close", 0) < cfg.min_price:
        return None
    if universe_row.get("dollar_volume", 0) < cfg.min_dollar_volume:
        return None
    if universe_row.get("marketCap", 0) < cfg.min_market_cap:
        return None
    if universe_row.get("turnover_rate", 0) < cfg.min_turnover_rate:
        return None
    if universe_row.get("tier") in cfg.exclude_tiers:
        return None
    return ohlcv
```

Existing `passes_bullish_gate` / `passes_bearish_gate` in `ta_prefilter.py` still run after this — they handle trend direction (close vs ma_20, rsi > 50), not liquidity/size.

## 7. Branch & migration

### Branch strategy

**`feat/apex-r2-etl`** off `master`, not off `trend-scan-cleanup`. Cleaner review surface; avoids partial-delete ghosts.

### Salvage from `trend-scan-cleanup` (port by hand, don't cherry-pick)

| Survives                                                                | Destination                            | Reason                                  |
| ----------------------------------------------------------------------- | -------------------------------------- | --------------------------------------- |
| `scripts/clients/massive_client.py` + tests                             | unchanged                              | foundational REST client                |
| `scripts/trend_scan_lib/stages/*` (bull/bear mirroring, volume profile) | unchanged                              | pure logic, data-source independent     |
| `scripts/trend_scan.py` Stage A/B caps + pure-TA Stage A                | ported, `_resolve_universe` repurposed | aligns with new arch                    |
| `scripts/ta_lib/indicators.py` (TA-Lib wrappers)                        | unchanged                              | reused by Action                        |
| `scripts/ta_lib/service.py::read_only` classmethod                      | ported pattern                         | useful for parquet-backed read path     |
| `pyproject.toml` `e2e` marker                                           | ported                                 | still want Massive + R2 E2E tests in CI |

### Discarded

`ta_premarket_prep.py`, `ta_premarket_status.py`, `ta_reseed_massive.py`, bars.py fetch-inside-TAService coupling, FastAPI scheduler hook, `ta_premarket_status.json`, `ta_premarket_universe.json`.

### Rollout sequence

1. **Build** — `feat/apex-r2-etl` branch. Implement Action + r2_store + parquet_store + apex_sync + TAService refactor. Unit tests green locally.
2. **Dry run** — `python3.13 scripts/apex_refresh.py --mode full --dry-run` writes to `data/apex_mirror_preview/`. Verify shape/schemas.
3. **Coordinate with bucket co-owners** — before manual refresh, confirm two things:
   (a) the external producer has stopped writing `parquet/historical/` (last write 2026-03-05 suggests yes);
   (b) the `signals/` pipeline either doesn't depend on the HKT tz label on historical timestamps, or can be updated to read `tz=UTC`. Our rewrite will flip the schema from `Asia/Hong_Kong` to `UTC` — daily dates are unchanged but hourly wall-clock values shift by 13h. Document contacts/owners in the PR.
4. **Manual first full refresh** — push branch; trigger Action via `workflow_dispatch --mode=full`. ~30 min. Verify R2 contents: 2,308 × 2 historical + indicators parquets, `meta/last_updated.json` updated.
5. **Shadow scan** — run `scripts/trend_scan.py` locally against the new mirror. Compare top-25 candidates vs a baseline scan (>60% overlap; investigate significant divergence).
6. **Enable cron** — merge PR; cron starts; FastAPI scheduler hook for `ta_premarket_prep` removed in the same PR.
7. **One-week soak** — monitor Action runs, scanner output, R2 storage cost. Keep `trend-scan-cleanup` branch parked as rollback anchor.
8. **Cleanup** — after one clean week: `git branch -D trend-scan-cleanup`, delete local `data/ta.duckdb`, prune any remaining `scripts/ta_premarket_*` refs.

### Risks & mitigations

| Risk                                                                                  | Mitigation                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| External producer resumes writing `parquet/historical/`                               | Confirm disablement before step 4. If we can't confirm, scope to indicators-only (retreat).                                                                                                        |
| Massive plan can't sustain nightly load                                               | Incremental is nearly free; full is weekend-only. If budget tight, make full monthly.                                                                                                              |
| Parquet schema drift breaks scanner mid-deploy                                        | `schema_version` in manifest; scanner aborts on unknown version.                                                                                                                                   |
| R2 manifest ETag race                                                                 | Conditional PUT with retry. Action is only writer; overlapping runs rare.                                                                                                                          |
| GH Actions secrets exposure                                                           | Use environment protection rule requiring review for workflow changes; rotate R2 keys quarterly.                                                                                                   |
| `signals/` pipeline assumes HKT-labelled timestamps and breaks when we rewrite as UTC | Before step 4, confirm with signals/ owner. If they can't adapt, add a `parquet_historical_v1_compat/` mirror written in HKT for them. Prefer making them the one to fix — the HKT label is wrong. |

## 8. Testing

| Layer                            | Framework                        | Coverage                                                                            |
| -------------------------------- | -------------------------------- | ----------------------------------------------------------------------------------- |
| Unit — R2 wrappers               | pytest + `moto` (or `responses`) | retry/backoff, 404, conditional PUT ETag                                            |
| Unit — parquet_store             | pytest + pyarrow in-memory       | roundtrip, UTC enforcement, dedupe-on-append                                        |
| Unit — apex_sync                 | pytest + mocked R2               | skip-when-fresh, parallel download, partial-sync recovery                           |
| Unit — apex_refresh              | pytest + mocked MassiveClient    | incremental vs full mode, per-ticker isolation, `data_quality.json` accumulation    |
| Unit — Stage A gate              | pytest                           | all floors + tier exclusions + missing fields                                       |
| E2E (`@pytest.mark.e2e`, opt-in) | pytest                           | MassiveClient live (exists); R2 round-trip on `test/` prefix; 5-ticker Action smoke |
| Regression                       | golden file diff                 | canned parquet mirror → top-25 byte-stable                                          |

Coverage target: 95% line coverage across `apex_refresh`, `r2_store`, `parquet_store`, `apex_sync`, modified `service.py`, Stage A. Per CLAUDE.md mandate.

## 9. Docs updates

- **Root `CLAUDE.md`** — Data Source Priority: add "R2 `apex-data` bucket for pre-computed OHLCV + TA (read in scanner; written by nightly Action)". Credentials table: add `R2_*` row.
- **`scripts/CLAUDE.md`** — replace `ta_premarket_prep` section with `apex_refresh` + `apex_sync` description. Update command table: drop `ta-premarket-prep`, `ta-reseed-massive`; add `apex-refresh` (local dry-run) and note nightly Action.
- **`docs/runbooks/apex-r2-cutover.md`** (NEW) — operator runbook mirroring `massive-cutover.md` but for the Action-based flow: how to trigger manual refresh, how to inspect failures, how to roll back.
- **`docs/architecture/data-files.md`** — replace `data/ta.duckdb` entry with `data/apex_mirror/` tree.
- **`CHANGELOG.md`** — top entry summarizing the migration.

## 10. Out of scope (explicit)

- Migrating other xenon pipelines (`uw_scan`, `cri_scan`, `portfolio sync`) to R2 — separate projects.
- `signals/` consumption — that pipeline's output (dual_macd_history, trend_state) could enrich scanner later, but not in this spec.
- External producer re-enablement — if disabled, leaving it disabled is assumed.
- Additional timeframes (`4h`, `30m`, `1w`) — scanner only needs `1d`+`1h` today. Extending is mechanical if needed later.
- Multi-region R2 replication, bucket versioning, lifecycle rules.

## 11. Open questions

None at spec approval time. All design questions resolved during brainstorm:

- ✅ Storage format: per-ticker × timeframe Parquet (matches existing bucket)
- ✅ Cadence: incremental weeknights, full Saturdays
- ✅ Universe: read from `meta/universe.json` (external)
- ✅ Scope: Action owns `parquet/historical/` + `parquet/indicators/` + manifest
- ✅ TA path: `parquet/indicators/{tf}/{TICKER}.parquet`
- ✅ Branch: new `feat/apex-r2-etl` off master; `trend-scan-cleanup` retired post-soak
