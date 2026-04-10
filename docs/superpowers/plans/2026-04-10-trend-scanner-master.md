# Trend Scanner — Master Implementation Plan

> **For agentic workers:** This is a master plan with 5 sub-plans. Execute each sub-plan in order using superpowers:subagent-driven-development or superpowers:executing-plans. Each sub-plan produces working, testable software independently.

**Goal:** Build a 3-stage pre-market trend scanner (TA → options structure → flow confirmation) that replaces the existing scanner page.

**Architecture:** Shared foundation layer (`scanner_lib/`) extracted from `uw_scan_lib/`, new `trend_scan_lib/` with staged pipeline, DuckDB storage for backtesting, web integration via existing FastAPI/Next.js patterns.

**Tech Stack:** Python 3.14, pytest, DuckDB, FastAPI, Next.js, TypeScript, Vitest

**Spec:** `docs/superpowers/specs/2026-04-10-trend-scanner-design.md`

---

## Sub-Plans

Execute in order — each sub-plan depends on the previous one being complete.

### Sub-Plan 1: Scanner Foundation (`scanner_lib/`)

**File:** `2026-04-10-trend-scanner-1-foundation.md`
**Scope:** Extract shared primitives from `uw_scan_lib/` into `scanner_lib/`. Models, universe loader, parallel executor, JSON cache writer, base scoring. Refactor `uw_scan` to import from `scanner_lib/`. All existing `uw_scan` tests must still pass.

**Produces:** `scripts/scanner_lib/` with 5 modules, `uw_scan_lib/` updated to use shared imports. Zero behavior change in `uw_scan`.

### Sub-Plan 2: Universe + Stage A (`trend_scan_lib/` part 1)

**File:** `2026-04-10-trend-scanner-2-universe-stage-a.md`
**Scope:** Triple-source universe builder (static indexes + UW flow + IB scanner), TA prefilter with 9 indicators, bullish/bearish gates, breakout detection. Static index files. Runnable as a standalone Stage A filter.

**Produces:** `scripts/trend_scan_lib/universe.py`, `scripts/trend_scan_lib/stages/ta_prefilter.py`, `data/universe/sp500.json`, `data/universe/nasdaq100.json`. Can filter 800 tickers to ~150 trend survivors.

### Sub-Plan 3: Stages B + C + Ranking (`trend_scan_lib/` part 2)

**File:** `2026-04-10-trend-scanner-3-stages-bc-ranking.md`
**Scope:** Options structure scoring, volatility scoring, flow confirmation scoring, trade type suggestion, composite ranking with min threshold gates, news sanity check flags.

**Produces:** `scripts/trend_scan_lib/stages/options_structure.py`, `volatility.py`, `flow_confirmation.py`, `scripts/trend_scan_lib/ranking.py`. Full 3-stage pipeline produces ranked candidates.

### Sub-Plan 4: Storage + CLI Entry Point

**File:** `2026-04-10-trend-scanner-4-storage-cli.md`
**Scope:** DuckDB schema + writer, JSON cache output, `trend_scan.py` CLI entry point with `--top N` flag, `TrendScanConfig`, end-to-end pipeline wiring.

**Produces:** `scripts/trend_scan_lib/storage.py`, `scripts/trend_scan.py`. Runnable via `python scripts/trend_scan.py --top 25`, outputs to DuckDB + JSON.

### Sub-Plan 5: Web Integration

**File:** `2026-04-10-trend-scanner-5-web.md`
**Scope:** FastAPI `POST /trend-scan` route, Next.js API route update, `ScannerSections` component replacement (score bars, direction badge, trade chip, expandable rows, sorting), scheduled 8:30 AM ET pre-market run.

**Produces:** Updated scanner page with full trend scanner UI, scheduled pre-market execution.

---

## TDD Protocol

Every sub-plan enforces strict TDD:

1. Write failing test
2. Run it — confirm FAIL
3. Write minimal implementation
4. Run it — confirm PASS
5. Commit

No implementation code is written before its test exists.
