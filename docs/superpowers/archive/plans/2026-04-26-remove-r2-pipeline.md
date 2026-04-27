# Remove R2 Market Data Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the Massive → R2 → local mirror data pipeline, its GitHub Action, the ta_lib package, and the trend scan scheduler. Keep trend scanner and Massive client shells for future repurposing.

**Architecture:** Pure deletion + surgical edits to server.py, pyproject.toml, web scanner route, and CLAUDE.md files. No new code beyond deprecation headers.

**Tech Stack:** Python (FastAPI), TypeScript (Next.js), GitHub Actions YAML, Markdown

---

### Task 1: Delete ta_lib package and fetcher

**Files:**

- Delete: `src/xenon/ta_lib/` (entire directory — 8 modules + `__pycache__/`)
- Delete: `src/xenon/fetchers/fetch_apex_data.py`
- Delete: `scripts/ta_lib/` (shim directory)

- [ ] **Step 1: Delete src/xenon/ta_lib/**

```bash
rm -rf src/xenon/ta_lib/
```

- [ ] **Step 2: Delete fetch_apex_data.py**

```bash
rm src/xenon/fetchers/fetch_apex_data.py
```

- [ ] **Step 3: Delete scripts/ta_lib/ shim**

```bash
rm -rf scripts/ta_lib/
```

- [ ] **Step 4: Verify no surviving imports from deleted modules in non-deprecated code**

```bash
grep -rn "from xenon.ta_lib\|import xenon.ta_lib\|from xenon.fetchers.fetch_apex_data\|from scripts.ta_lib" src/xenon/ --include='*.py' | grep -v scanners/trend | grep -v clients/massive
```

Expected: no output (trend scanner and massive client are deprecated — their broken imports are expected).

- [ ] **Step 5: Commit**

```bash
git add -A src/xenon/ta_lib/ src/xenon/fetchers/fetch_apex_data.py scripts/ta_lib/
git commit -m "remove: ta_lib package, fetch_apex_data entrypoint, scripts/ta_lib shim"
```

---

### Task 2: Delete tests for removed modules

**Files:**

- Delete: `scripts/tests/test_ta_lib/` (6 test modules + `__pycache__/`)
- Delete: `scripts/tests/test_trend_scan_lib/` (test_universe_mirror + `__pycache__/`)
- Delete: `scripts/tests/test_r2_store.py`
- Delete: `scripts/tests/test_r2_smoke.py`
- Delete: `scripts/tests/test_apex_sync.py`
- Delete: `scripts/tests/test_apex_refresh.py`

- [ ] **Step 1: Delete test directories**

```bash
rm -rf scripts/tests/test_ta_lib/
rm -rf scripts/tests/test_trend_scan_lib/
```

- [ ] **Step 2: Delete individual test files**

```bash
rm scripts/tests/test_r2_store.py
rm scripts/tests/test_r2_smoke.py
rm scripts/tests/test_apex_sync.py
rm scripts/tests/test_apex_refresh.py
```

- [ ] **Step 3: Commit**

```bash
git add -A scripts/tests/test_ta_lib/ scripts/tests/test_trend_scan_lib/ scripts/tests/test_r2_store.py scripts/tests/test_r2_smoke.py scripts/tests/test_apex_sync.py scripts/tests/test_apex_refresh.py
git commit -m "remove: tests for ta_lib, r2_store, apex_sync, apex_refresh"
```

---

### Task 3: Delete GitHub Action workflow

**Files:**

- Delete: `.github/workflows/apex-data-refresh.yml`

- [ ] **Step 1: Delete the workflow file**

```bash
rm .github/workflows/apex-data-refresh.yml
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/apex-data-refresh.yml
git commit -m "remove: apex-data-refresh GitHub Action workflow"
```

---

### Task 4: Remove trend scan scheduler and route from server.py

**Files:**

- Modify: `src/xenon/api/server.py`

The trend scan scheduler is three pieces in server.py:

1. `_trend_scan_premarket_loop()` function (lines 143-167)
2. Task creation in lifespan (lines 424-427)
3. Task cancellation in lifespan shutdown (lines 441-446)
4. `POST /trend-scan` route (lines 1288-1295)

- [ ] **Step 1: Delete \_trend_scan_premarket_loop function**

Remove the entire function (lines 143-167):

```python
async def _trend_scan_premarket_loop():
    """Run trend scanner at 8:30 AM ET on weekdays."""
    import zoneinfo

    et = zoneinfo.ZoneInfo("America/New_York")
    while True:
        now = datetime.now(et)
        target_hour, target_min = 8, 30
        target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        while target.weekday() >= 5:
            target += timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        logger.info("Trend scan scheduled for %s (in %.0fs)", target, wait_secs)
        await asyncio.sleep(wait_secs)
        try:
            result = await run_entry_point("xenon-trend-scan", ["--top", "25"], timeout=180)
            if result.ok:
                _write_cache(DATA_DIR / "trend_scan.json", result.data)
                logger.info("Pre-market trend scan complete: %d candidates", len(result.data.get("candidates", [])))
            else:
                logger.warning("Pre-market trend scan failed: %s", result.error)
        except Exception:
            logger.warning("Pre-market trend scan error", exc_info=True)
```

- [ ] **Step 2: Remove trend scan task creation in lifespan**

Remove the comment and task creation block (lines 424-427):

```python
    # Trend scanner (8:30 AM ET weekdays)
    _trend_scan_task = None
    if os.environ.get("XENON_DAILY_JOB_WORKER_ID", "0") == "0":
        _trend_scan_task = asyncio.create_task(_trend_scan_premarket_loop())
```

- [ ] **Step 3: Remove trend scan task cancellation in lifespan shutdown**

Remove the cancellation block (lines 441-446):

```python
        if _trend_scan_task is not None:
            _trend_scan_task.cancel()
            try:
                await _trend_scan_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
```

- [ ] **Step 4: Remove POST /trend-scan route**

Remove the entire route handler (lines 1288-1295):

```python
@app.post("/trend-scan")
async def trend_scan():
    """Run 3-stage trend scanner (trend_scan.py --top 25)."""
    result = await run_entry_point("xenon-trend-scan", ["--top", "25"], timeout=180)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    _write_cache(DATA_DIR / "trend_scan.json", result.data)
    return result.data
```

- [ ] **Step 5: Verify server.py has no remaining trend_scan references**

```bash
grep -n "trend_scan\|trend.scan\|/trend-scan" src/xenon/api/server.py
```

Expected: no output.

- [ ] **Step 6: Verify FastAPI starts without errors**

```bash
timeout 5 uv run python -c "from xenon.api.server import app; print('OK')" 2>&1 || true
```

Expected: `OK` (import succeeds; timeout kills the process before uvicorn starts).

- [ ] **Step 7: Commit**

```bash
git add src/xenon/api/server.py
git commit -m "remove: trend scan scheduler and POST /trend-scan route from server.py"
```

---

### Task 5: Update pyproject.toml

**Files:**

- Modify: `pyproject.toml`

- [ ] **Step 1: Remove xenon-fetch-apex-data entry point**

Remove this line from `[project.scripts]`:

```toml
xenon-fetch-apex-data       = "xenon.fetchers.fetch_apex_data:main"
```

- [ ] **Step 2: Remove ta_lib from isort known-first-party**

In `[tool.ruff.lint.isort]`, remove this line:

```toml
    "ta_lib",  # active — consumed by fetchers/fetch_apex_data, scanners/trend/cli, etc.
```

- [ ] **Step 3: Remove ta-lib from test dependencies**

In `[project.optional-dependencies]`, remove this line from `test`:

```toml
    "ta-lib>=0.4",
```

No surviving code imports `talib`. Removing avoids building the TA-Lib C library in CI.

- [ ] **Step 4: Verify pyproject.toml is valid**

```bash
uv run python -c "import tomllib; tomllib.load(open('pyproject.toml', 'rb')); print('valid')"
```

Expected: `valid`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "remove: fetch-apex-data entry point, ta_lib from isort + test deps"
```

---

### Task 6: Update web scanner route + useScanner hook + tests

**Files:**

- Modify: `web/app/api/scanner/route.ts`
- Modify: `web/lib/useScanner.ts`
- Modify: `web/tests/fastapi-migration.test.ts`

The GET handler stays (reads cached JSON file, returns empty state if missing). The POST handler is removed — it calls the deleted FastAPI route. The useScanner hook must switch to GET-only mode so the refresh button doesn't 405.

- [ ] **Step 1: Remove POST handler from route.ts**

Remove the entire POST export (lines 60-85) from `web/app/api/scanner/route.ts`:

```typescript
export async function POST(): Promise<Response> {
  try {
    const data = await xenonFetch("/trend-scan", {
      method: "POST",
      timeout: 200_000,
    });
    const cache_meta = buildCacheMeta(CACHE_PATH);
    return NextResponse.json({ ...data, cache_meta });
  } catch (error) {
    // Serve cached data on failure
    try {
      const raw = await readFile(CACHE_PATH, "utf-8");
      const cached = JSON.parse(raw);
      const cache_meta = buildCacheMeta(CACHE_PATH);
      const res = NextResponse.json({ ...cached, cache_meta, is_stale: true });
      res.headers.set(
        "X-Sync-Warning",
        "Xenon API unavailable - serving cached data",
      );
      return res;
    } catch {
      const message = error instanceof Error ? error.message : "Scanner failed";
      return NextResponse.json({ error: message }, { status: 502 });
    }
  }
}
```

Also remove the unused `xenonFetch` import if it's only used by POST:

```typescript
import { xenonFetch } from "@/lib/xenonApi";
```

(Verify first: `grep xenonFetch web/app/api/scanner/route.ts` — if only used in the POST handler, remove the import.)

- [ ] **Step 2: Set hasPost: false in useScanner config**

In `web/lib/useScanner.ts`, update the config to disable POST-based sync. The `useSyncHook` defaults `hasPost: true`, which would cause refresh clicks to POST to the now-deleted endpoint and 405.

Change:

```typescript
const config = {
  endpoint: "/api/scanner",
  extractTimestamp: (d: ScannerData) => d.scan_timestamp || null,
};
```

to:

```typescript
const config = {
  endpoint: "/api/scanner",
  extractTimestamp: (d: ScannerData) => d.scan_timestamp || null,
  hasPost: false,
};
```

- [ ] **Step 3: Remove POST scanner tests from fastapi-migration.test.ts**

Delete the entire `POST /api/scanner` describe block (lines 106-165) from `web/tests/fastapi-migration.test.ts`:

```typescript
// =============================================================================
// POST /api/scanner — success + cache fallback
// =============================================================================

describe("POST /api/scanner (via xenonFetch)", () => {
  it("returns data on success", async () => {
    ...
  });

  it("falls back to cached data on xenonFetch failure", async () => {
    ...
  });

  it("returns 502 on failure when no cache exists", async () => {
    ...
  });
});
```

- [ ] **Step 4: Verify TypeScript compiles**

```bash
cd web && npx tsc --noEmit 2>&1 | head -20
```

Expected: no errors related to scanner route.

- [ ] **Step 5: Run web tests**

```bash
cd web && npm test -- --reporter=verbose 2>&1 | tail -30
```

Expected: all pass. GET tests in `route-cache-meta.test.ts` and `fastapi-migration.test.ts` still work.

- [ ] **Step 6: Commit**

```bash
git add web/app/api/scanner/route.ts web/lib/useScanner.ts web/tests/fastapi-migration.test.ts
git commit -m "remove: POST scanner route, switch useScanner to GET-only, remove POST tests"
```

---

### Task 7: Add deprecation notices

**Files:**

- Modify: `src/xenon/clients/massive_client.py` (add deprecation header)
- Modify: `src/xenon/scanners/trend/cli.py` (add deprecation header)

- [ ] **Step 1: Add deprecation notice to massive_client.py**

Replace the module docstring (line 1) with:

```python
"""DEPRECATED — Massive.com REST client for historical OHLCV aggregates.

The R2 pipeline that consumed this client was removed 2026-04-26.
This module is retained for future repurposing. Imports will still work
but the client is not called by any active code path.

Original scope (v1): /v2/aggs/ticker/{T}/range/{m}/{timespan}/{from}/{to}
Timeframes: 1d, 1h. Returns ET-normalized OHLCV + VWAP + tx_count.
"""
```

- [ ] **Step 2: Add deprecation notice to trend/cli.py**

Replace the module docstring (line 1) with:

```python
"""DEPRECATED — Trend scanner CLI and pipeline orchestration.

The R2/ta_lib data source was removed 2026-04-26. This module's imports
from xenon.ta_lib will fail at runtime. Retained for future repurposing
with a different data source.
"""
```

- [ ] **Step 3: Commit**

```bash
git add src/xenon/clients/massive_client.py src/xenon/scanners/trend/cli.py
git commit -m "deprecate: massive_client and trend scanner cli (R2 pipeline removed)"
```

---

### Task 8: Update CI workflow to skip deprecated tests

**Files:**

- Modify: `.github/workflows/ci.yml`

The `pytest --collect-only` smoke test (line 100) and full `uv run pytest` (line 106) will fail when deprecated trend scanner tests try to import deleted `ta_lib`. Add `--ignore` flags matching the local verification step.

- [ ] **Step 1: Add --ignore flags to collection smoke test**

Update line 100 from:

```yaml
run: uv run pytest --collect-only -q
```

to:

```yaml
run: >-
  uv run pytest --collect-only -q
  --ignore=scripts/tests/test_trend_scan.py
  --ignore=scripts/tests/test_trend_scan_runtime.py
  --ignore=scripts/tests/test_trend_config.py
  --ignore=scripts/tests/test_trend_models.py
  --ignore=scripts/tests/test_trend_storage.py
  --ignore=scripts/tests/test_trend_ranking.py
  --ignore=scripts/tests/test_trend_scan_catalysts.py
  --ignore=scripts/tests/test_ta_prefilter.py
  --ignore=scripts/tests/test_flow_confirmation.py
  --ignore=scripts/tests/test_volatility.py
```

- [ ] **Step 2: Add --ignore flags to full pytest run**

Update the full suite run (line 106) from:

```yaml
uv run pytest
```

to:

```yaml
uv run pytest --ignore=scripts/tests/test_trend_scan.py --ignore=scripts/tests/test_trend_scan_runtime.py --ignore=scripts/tests/test_trend_config.py --ignore=scripts/tests/test_trend_models.py --ignore=scripts/tests/test_trend_storage.py --ignore=scripts/tests/test_trend_ranking.py --ignore=scripts/tests/test_trend_scan_catalysts.py --ignore=scripts/tests/test_ta_prefilter.py --ignore=scripts/tests/test_flow_confirmation.py --ignore=scripts/tests/test_volatility.py
```

Also add the same `--ignore` flags to the affected-tests run (line 104) by appending them after the `--base` argument.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: skip deprecated trend scanner tests after ta_lib removal"
```

---

### Task 9: Update CLAUDE.md documentation

**Files:**

- Modify: `CLAUDE.md` (root)
- Modify: `src/xenon/CLAUDE.md`
- Modify: `src/xenon/api/CLAUDE.md`

- [ ] **Step 1: Update root CLAUDE.md — Scanner Hierarchy**

Replace the `src/xenon/scanners/trend/` line (line 19):

```markdown
- `src/xenon/scanners/trend/` (entry: `xenon-trend-scan`) — 3-stage pre-market trend scanner, DuckDB-backed (`data/trend_scan.duckdb`). Auto-runs 8:30 AM ET weekdays via the FastAPI scheduler.
```

with:

```markdown
- `src/xenon/scanners/trend/` (entry: `xenon-trend-scan`) — **DEPRECATED.** Code retained for repurposing; R2/ta_lib data source removed 2026-04-26. Scheduler removed from server.py.
```

- [ ] **Step 2: Update root CLAUDE.md — Data Source Priority**

Replace lines 27-29:

```markdown
1. Interactive Brokers — real-time quotes, chains, execution, live portfolio
2. **Cloudflare R2 `apex-data` bucket** — pre-computed OHLCV + TA indicators (nightly, via GitHub Action `apex-data-refresh`). Read-only in the scanner.
3. Massive.com — historical OHLCV source. Action-side only; the trend scanner never calls Massive directly at scan time.
4. Unusual Whales (`$UW_TOKEN`) — dark pool, sweeps, alerts (Stage B/C).
```

with:

```markdown
1. Interactive Brokers — real-time quotes, chains, execution, live portfolio
2. Unusual Whales (`$UW_TOKEN`) — dark pool, sweeps, alerts (Stage B/C).
```

- [ ] **Step 3: Update root CLAUDE.md — remove "Never use Yahoo Finance" Massive reference**

Replace line 33:

```markdown
**Never use Yahoo Finance.** Historical data flows Massive → R2 → scanner.
```

with:

```markdown
**Never use Yahoo Finance.**
```

- [ ] **Step 4: Update root CLAUDE.md — Startup Checklist**

Remove line 109:

```markdown
- [ ] Pre-market trend scan runs 8:30 AM ET weekdays → `data/trend_scan.json`
```

- [ ] **Step 5: Update root CLAUDE.md — CI section**

Remove line 180:

```markdown
- `apex-data-refresh.yml` — nightly R2 OHLCV/TA refresh.
```

- [ ] **Step 6: Update src/xenon/CLAUDE.md — Data Source Priority**

Replace lines 9-15:

```markdown
1. Interactive Brokers (TWS/Gateway) — real-time quotes, chains, execution, live portfolio
2. Cloudflare R2 `apex-data` bucket — pre-computed OHLCV + TA indicators (read-only for scanner; written by nightly GitHub Action)
3. Massive.com (`$MASSIVE_API_KEY`) — historical OHLCV source, Action-side only
4. Unusual Whales (`$UW_TOKEN`) — dark pool, sweeps, alerts
5. Web scrape — last resort

**Never use Yahoo Finance.** Scanner never calls Massive directly.
```

with:

```markdown
1. Interactive Brokers (TWS/Gateway) — real-time quotes, chains, execution, live portfolio
2. Unusual Whales (`$UW_TOKEN`) — dark pool, sweeps, alerts
3. Web scrape — last resort

**Never use Yahoo Finance.**
```

- [ ] **Step 7: Update src/xenon/CLAUDE.md — Scanner Libs section**

Replace the `scanners/trend/` bullet (line 24):

```markdown
- `src/xenon/scanners/trend/` — 3-stage pre-market trend scanner (entry: `xenon-trend-scan`). Stages: `ta_prefilter` → `options_structure` + `volatility` + `flow_confirmation`. Config: `config.py`. Storage: DuckDB (`data/trend_scan.duckdb`) via `storage.py` — `duckdb` package imported lazily so scanners that don't need persistence still run.
```

with:

```markdown
- `src/xenon/scanners/trend/` — **DEPRECATED.** Code retained for repurposing; R2/ta_lib data source removed 2026-04-26.
```

Remove the entire `ta_lib` bullet (line 26):

```markdown
- `src/xenon/ta_lib/` — Cloudflare R2 parquet-mirror reader. `r2_store.py` (sole owner of boto3 S3 calls), `parquet_store.py` (pyarrow I/O, UTC enforcement, HKT→UTC legacy normalization, daily-bar UTC-midnight per spec), `apex_sync.py` (scanner-side R2 mirror download gated by `meta/last_updated.json`, atomic tmp→rename swap, R2-outage fallback), `dry_run_store.py` (local-filesystem stand-in for `--dry-run`), `service.py` (`TAService` read-through view; full snapshot contract preserved). `indicators.py` (TA-Lib wrappers) and `bars.py` (Massive→OHLCV adapter) run in the GitHub Action, not the scanner. Mirror on disk: `data/apex_mirror/`.
```

- [ ] **Step 8: Update src/xenon/CLAUDE.md — Dev Environment example**

Replace line 111:

```bash
uv run xenon-trend-scan --top 25 # run any CLI entry point
```

with:

```bash
uv run xenon-uw-scan --top 25    # run any CLI entry point
```

- [ ] **Step 9: Update src/xenon/CLAUDE.md — Commands table**

Replace the `trend-scan` row (line 132):

```markdown
| `trend-scan` | 3-stage pre-market trend scanner (TA prefilter → structure/vol/flow). DuckDB-backed. Auto-runs 8:30 AM ET weekdays. |
```

with:

```markdown
| `trend-scan` | **DEPRECATED.** Code retained for repurposing; R2/ta_lib data source removed 2026-04-26. |
```

Remove the `apex-refresh` row (line 153):

```markdown
| `apex-refresh` | Apex R2 ETL entrypoint. Nightly GitHub Action (`.github/workflows/apex-data-refresh.yml`). Local dry-run: `xenon-fetch-apex-data --mode full --dry-run --timeframes 1d,1h`. Writes OHLCV + TA-indicator parquets to R2 `apex-data` bucket. |
```

- [ ] **Step 10: Update src/xenon/api/CLAUDE.md — Module Layout**

Replace line 11:

```markdown
- `server.py` — endpoint dispatch, IB pool lifecycle, background schedulers (pre-market trend scan 8:30 AM ET, CTA sync)
```

with:

```markdown
- `server.py` — endpoint dispatch, IB pool lifecycle, background schedulers (CTA sync)
```

- [ ] **Step 11: Update src/xenon/api/CLAUDE.md — Background Tasks**

Remove the pre-market trend scan bullet (line 28):

```markdown
- **Pre-market trend scan** — 8:30 AM ET weekdays, `xenon-trend-scan --top 25`, writes `data/trend_scan.json`. Defined as an asyncio loop started in the lifespan handler (`_trend_scan_premarket_loop`).
```

- [ ] **Step 12: Commit**

```bash
git add CLAUDE.md src/xenon/CLAUDE.md src/xenon/api/CLAUDE.md
git commit -m "docs: update CLAUDE.md files — remove R2/Massive/trend-scan references"
```

---

### Task 10: Clean up local data files

**Files:**

- Delete: `data/apex_mirror/` (local, not in git)
- Delete: `data/apex_mirror_preview/` (local, not in git)
- Delete: `data/trend_scan.json` (local, not in git)
- Delete: `data/trend_scan.duckdb` (local, not in git)

- [ ] **Step 1: Remove local data files**

```bash
rm -rf data/apex_mirror/ data/apex_mirror_preview/
rm -f data/trend_scan.json data/trend_scan.duckdb
```

These are gitignored, so no commit needed.

---

### Task 11: Final verification

- [ ] **Step 1: Run Python tests (excluding deprecated trend scanner tests)**

```bash
uv run pytest --ignore=scripts/tests/test_trend_scan.py --ignore=scripts/tests/test_trend_scan_runtime.py --ignore=scripts/tests/test_trend_config.py --ignore=scripts/tests/test_trend_models.py --ignore=scripts/tests/test_trend_storage.py --ignore=scripts/tests/test_trend_ranking.py --ignore=scripts/tests/test_trend_scan_catalysts.py --ignore=scripts/tests/test_ta_prefilter.py --ignore=scripts/tests/test_flow_confirmation.py --ignore=scripts/tests/test_volatility.py -x 2>&1 | tail -20
```

Expected: all pass. Zero import errors from surviving code.

- [ ] **Step 2: Run web typecheck**

```bash
cd web && npx tsc --noEmit 2>&1 | tail -10
```

Expected: no errors.

- [ ] **Step 3: Run web tests**

```bash
cd web && npm test 2>&1 | tail -20
```

Expected: all pass.

- [ ] **Step 4: Verify FastAPI app imports cleanly**

```bash
timeout 5 uv run python -c "from xenon.api.server import app; print('server OK')" 2>&1 || true
```

Expected: `server OK`.

- [ ] **Step 5: Verify no accidental deletions of untouched files**

```bash
test -d src/xenon/scanners/_shared && echo "_shared OK"
test -d src/xenon/scanners/uw && echo "uw OK"
test -f src/xenon/clients/massive_client.py && echo "massive_client OK"
test -d src/xenon/scanners/trend && echo "trend dir OK"
test -f web/app/scanner/page.tsx && echo "scanner page OK"
```

Expected: all print OK.
