# Remove R2 Market Data Pipeline

**Date:** 2026-04-26
**Status:** Approved
**Motivation:** Cost/simplification. Alternative data sources available. The Massive -> R2 -> local mirror pipeline is no longer needed.

## Scope

Remove the nightly GitHub Action that fetches OHLCV from Massive, computes TA indicators, and uploads parquet files to Cloudflare R2. Remove the scanner-side sync layer that mirrors R2 to disk. Remove the ta_lib package that owns all of this. Keep the trend scanner and Massive client shells for future repurposing.

## Files to Delete

### Core modules

- `src/xenon/ta_lib/` — entire directory (8 modules: `__init__.py`, `r2_store.py`, `parquet_store.py`, `apex_sync.py`, `service.py`, `bars.py`, `indicators.py`, `dry_run_store.py`)
- `src/xenon/fetchers/fetch_apex_data.py` — ETL entrypoint
- `scripts/ta_lib/` — compatibility shim directory

### GitHub Action

- `.github/workflows/apex-data-refresh.yml`

### Tests

- `scripts/tests/test_ta_lib/` — entire directory (6 test modules)
- `scripts/tests/test_trend_scan_lib/` — entire directory
- `scripts/tests/test_r2_store.py`
- `scripts/tests/test_r2_smoke.py`
- `scripts/tests/test_apex_sync.py`
- `scripts/tests/test_apex_refresh.py`

### Data files (not tracked in git, local cleanup)

- `data/apex_mirror/`
- `data/apex_mirror_preview/`
- `data/trend_scan.json`
- `data/trend_scan.duckdb`

## Files to Deprecate (keep with notice)

- `src/xenon/clients/massive_client.py` — add deprecation header; will be repurposed
- `src/xenon/scanners/trend/cli.py` — add deprecation header; imports from deleted `ta_lib` will break; module is inert until repurposed

## Files to Modify

### `src/xenon/api/server.py`

- Remove `_trend_scan_premarket_loop()` function (8:30 AM ET scheduler)
- Remove `_trend_scan_task` variable and its lifecycle (create on startup, cancel on shutdown)
- Remove `POST /trend-scan` route handler

### `pyproject.toml`

- Remove entry point: `xenon-fetch-apex-data = "xenon.fetchers.fetch_apex_data:main"`
- Keep entry point: `xenon-trend-scan` (inert, matches deprecated state)
- Remove `ta_lib` from isort `known_first_party` if present

### `web/app/api/scanner/route.ts`

- Remove POST handler that calls `/trend-scan` backend
- Keep GET handler or return empty response so page doesn't 500

### Documentation

- Root `CLAUDE.md`: update Data Source Priority (remove R2 + Massive entries), Scanner Hierarchy (note trend scanner deprecated), Startup Checklist (remove trend scan line), CI section (remove apex-data-refresh reference)
- `src/xenon/CLAUDE.md`: remove ta_lib and trend scanner references

## Explicitly Untouched

- `src/xenon/scanners/_shared/` — UW scanner depends on it
- `src/xenon/scanners/uw/` — no R2/ta_lib dependencies
- `web/app/scanner/page.tsx` — shell for future reuse
- `web/lib/useScanner.ts` — will render empty state
- `.env` — all keys stay (MASSIVE*API_KEY, R2*\* vars retained for future use)
- Trend scanner test files (`test_trend_scan.py`, `test_trend_scan_runtime.py`, `test_trend_config.py`, `test_trend_models.py`, `test_trend_storage.py`, `test_trend_ranking.py`, `test_trend_scan_catalysts.py`, `test_ta_prefilter.py`, `test_flow_confirmation.py`, `test_volatility.py`) — match deprecated state of scanner

## Verification

After all changes:

1. `uv run pytest` — no broken imports from surviving code (trend scanner tests may fail on `ta_lib` import, which is expected; exclude them or mark skip)
2. `cd web && npm test` — passes
3. `cd web && npx tsc --noEmit` — passes
4. FastAPI starts without errors (no trend scan scheduler)

## Risks

- **Trend scanner tests will fail** once `ta_lib` is deleted (they import from it). These tests match the deprecated state — either skip them in CI or delete them. Decision: skip via pytest marker or conftest guard.
- **`generate_universe_ts.py`** may reference ta_lib/universe — verify and handle.
- **No data regression** — R2 pipeline has no live consumers once the scheduler is removed. Scanner page will show empty state.
