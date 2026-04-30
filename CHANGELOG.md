# Changelog

All notable changes to Xenon are documented here. Format loosely based on
[Keep a Changelog](https://keepachangelog.com/) with semver-ish versioning.

## [Unreleased]

### Added

- **IB→Postgres activity mirror (#70).** Boot replay of IB executions into `xenon.order_fills` + aggregated `xenon.trades`; periodic poller (default 60s, env `XENON_IB_ACTIVITY_POLL_S`) runs open-order import + fill replay every tick with independent failure isolation. `register_from_snapshot` now UPDATEs `snapshot-*` rows on TWS price/qty drift and emits `IB_MIRROR_UPDATE` events. `record_external_fills` resolves `(perm_id, scope)` → `submission_id` so blotter rows tie back to their originating order.
- **`tif` column on `order_submissions` (#71).** Alembic migration `8a3f2c7d1e90` replaces the route's hard-coded `"DAY"` with the IB-reported TIF, threaded through writer + drift detection + reader. GTC orders now render correctly.
- **Snapshot row resurrection (#71).** `register_from_snapshot` restores rows in any terminal state (CANCELLED/FILLED/REJECTED/FAILED/UNKNOWN) when IB still reports them open. Emits `IB_MIRROR_RESURRECT` event for audit. Self-heals from cancel-route races and operator errors.
- **Leg-contract enrichment in rehydrate (#71).** `single_leg_rehydrate._enrich_records_via_ib` qualifies leg conIds via `IB.qualify_contracts` when `Fill.contract` lacks strike/right/expiry. Fixes "Bull Put Spread (Short Unknown Unknown / Long Unknown Unknown)" rendering on closing combos.
- **`docs/reference/order-path-incident-history.md`.** Chronological log of every non-trivial order-path bug since PR #27, with root cause, fix, and the regression test that protects against recurrence. Cross-linked from root + api CLAUDE.md.

### Fixed

- **Cancel doesn't clear Open Orders panel (#71).** `/orders/cancel` now calls `mark_terminal(state="CANCELLED", reason_code="USER_CANCEL")` on the success path. The activity poller intentionally cannot disambiguate fill vs cancel on disappearance, so the cancel route is the only authoritative trigger.
- **BAG combo missing from panel (#71).** `sync_open_orders_to_postgres` no longer drops orders with `orderId=0` (which IB returns for combos viewed from non-originating clientIds). `perm_id` alone is sufficient identity.
- **Trade aggregator labels combos as "Stock" (#71).** New `_has_bag_signal` heuristic — checks both `source.security_type` and per-fill `metadata.sec_type` — derives "Spread"/"Combo" from leg shape so snapshot-\* and legacy_id BAG groups never fall through to "Stock".

## [0.0.1] — 2026-04-24

- Versioning reset. Begin semver from `0.0.1` as part of introducing the CI/release/deploy pipeline.

## [Pre-0.0.1 history]

### Changed

- **Futu ticker navigation (feat/futu-ticker-chain-fixes).** `TickerLink` no longer short-circuits to a non-interactive `<span>` in read-only contexts — Futu rows now route into the ticker workspace like IB rows. Execution surfaces (order modal, order API) stay guarded deeper in the flow; the label on Futu rows still signals read-only via `aria-label`.
- **IB option subscriptions (feat/futu-ticker-chain-fixes).** `ib_realtime_server.js` now qualifies option contracts with `exchange="SMART"` and `currency="USD"` when subscribing. Fixes empty Delta/IV/Gamma columns in the options chain caused by node-ib rejecting under-specified option contracts.

### Added

- `web/tests/options-chain-0dte-selection.test.tsx` — documents the current contract: default expiry skips same-day options; manually selecting a 0DTE expiry loads the chain.

### Changed

- **Apex R2 ETL (feat/apex-r2-etl).** Historical OHLCV + TA-indicator computation moved out of the trend scanner into a nightly GitHub Action (`apex-data-refresh`). Scanner now reads pre-computed Parquet from Cloudflare R2 `apex-data` bucket via a local mirror at `data/apex_mirror/`. No Massive or UW calls at scan time for Stage A.
- New dependencies: `boto3`, `pyarrow`, `moto[s3]` (test). See `pyproject.toml`.
- New tribunal amendments A1–A22 shipped inline; see `docs/superpowers/plans/2026-04-16-apex-r2-etl.md` on `trend-scan-cleanup` anchor branch for the full audit trail.

### Added

- `scripts/ta_lib/r2_store.py` — Cloudflare R2 S3-compatible wrapper (ETag, typed errors, retry).
- `scripts/ta_lib/parquet_store.py` — pyarrow I/O with UTC enforcement, DST-safe HKT→UTC normalization, UTC-midnight daily bars.
- `scripts/ta_lib/apex_sync.py` — scanner-side mirror sync with atomic swap + R2-outage fallback.
- `scripts/ta_lib/dry_run_store.py` — local-filesystem stand-in for `R2Store` under `--dry-run`.
- `scripts/apex_refresh.py` — GitHub Action entrypoint. Parallel ThreadPoolExecutor driver, conditional-PUT manifest update with retry, session-completeness guard (A18).
- `.github/workflows/apex-data-refresh.yml` — nightly + Saturday-full cron + workflow_dispatch.
- `docs/runbooks/apex-r2-cutover.md` — operator runbook.

### Removed

- `scripts/ta_premarket_prep.py`, `scripts/ta_reseed_massive.py`, `scripts/ta_seed_yahoo.py`, `scripts/ta_cli.py`.
- `scripts/api_status/` directory (entire package).
- `data/ta.duckdb` runtime cache (superseded by parquet mirror).
- FastAPI scheduler hook for pre-market data prep (`_premarket_data_prep_loop` in `scripts/api/server.py`).
- Orphaned tests: `test_trend_scan_bearish.py`, `test_trend_scan_e2e.py`, `test_trend_universe.py`, `test_ta_lib/test_premarket_prep.py`, `test_ta_lib/test_seed_yahoo.py`, `test_ta_reseed_massive.py`, `test_ta_premarket_status.py`, `test_e2e_massive_pipeline.py`, `test_ta_lib/test_store_e2e.py`, `test_ta_lib/test_store.py`, `test_ta_lib/test_ta_cli_offline.py`, `test_trend_scan_status.py`.

### Follow-ups (post-soak)

- Update `docs/superpowers/specs/2026-04-16-apex-r2-etl-design.md` (on `trend-scan-cleanup` anchor) §6 to state the RSI threshold is `rsi > 40 / rsi < 60` (matching the implementation in `scripts/trend_scan_lib/stages/ta_prefilter.py:157,169`), not `rsi > 50` as originally written. This is amendment **A20**; documented here because the spec file itself is not on this branch.
- Retire `trend-scan-cleanup` branch after one clean week of the new pipeline.

### Previously [0.1.1] - 2026-04-16

#### Fixed — UW portfolio SSE cache preservation + daily stats reset at 8PM ET

Two coupled fixes to the Unusual Whales telemetry path.

##### SSE streaming no longer wipes cached tiles

`useUwPortfolio` now uses a two-Map architecture during streaming: SSE rows
accumulate independently, and the displayed state is a merge of cached tickers
(loaded from the on-disk snapshot) with incoming SSE tickers (SSE wins on
conflict). Previously, the first SSE row reset the visible list to a
single-element array, causing the rest of the portfolio's tiles to vanish for
several seconds until later events repopulated. Monotonicity now holds: the
visible ticker count never decreases during a stream. On a valid `done` event,
the snapshot cache is finalized to the SSE-only set (authoritative); incomplete
streams preserve the merged view so remounts don't drop tiles.

##### Daily stats aligned to UW's 8PM ET quota boundary

New `get_stats_with_daily()` and `get_daily_stats()` on the process-wide
`UWApiStats` singleton expose counters for the current UW daily quota window,
rolled up from hourly buckets. The sidebar now shows "UW Today" with daily
request count, cache-hit %, and 2xx/4xx/5xx breakdown — previously session
totals were displayed, which never reset and bore no relation to UW's 20k/day
budget ceiling. Boundary computation uses `ZoneInfo("America/New_York")` for
DST-correct wall-clock math. The `/uw-stats` endpoint returns session + daily
under a single lock to prevent torn snapshots under concurrent writes.

##### Tests

- `test_uw_api_stats_history.py`: 40 tests covering hour-boundary correctness,
  DST transitions in both directions, cache-hit exclusion from request count,
  zero-state behavior, and concurrent-write snapshot consistency.
- `useUwPortfolio.test.ts`: 8 tests covering cached-tile preservation during
  streaming, SSE-wins-on-conflict merge, `done`-gated finalization, and
  incomplete-stream cache behavior.

### Previously [0.1.0] - 2026-04-15

#### Added — Trend Scanner: bearish pipeline, catalyst stage, pre-market prep

Fourteen-commit feature branch addressing all nine findings from the
Codex+Gemini+Claude tribunal review of `feat/ta-integration`. Plan:
`docs/superpowers/plans/2026-04-14-trend-scanner-tribunal-fixes.md`.

##### Signal accuracy

- Breakout detection now requires `close >= high_20d` — consolidation
  narrowness alone no longer flags as breakout (Finding #3).
- Volume profile isolates up-day vs down-day volume via new
  `up_day_volume_ratio`, weighted 2× so distribution patterns penalize
  the trend score (Finding #9).
- Stage B rejects unsupported overhead walls: call wall within 2% above
  spot with no supportive put wall below now hard-fails like severe
  pinning (Finding #8).
- Snapshot exposes `high_20d`, `low_20d`, `low_52w`, `up_day_volume_ratio`
  — additive schema change, enables both breakout gate and bearish mirror.

##### Scope expansion

- **Bearish pipeline** runs alongside bullish (Finding #1). Stage A split
  into direction-neutral data fetch + direction-specific gate. Mirrored
  `passes_bearish_gate`, `detect_breakdown`,
  `has_unsupported_underhead_wall`. Structure / OI / flow scoring branch
  on direction. Live scan emits bullish AND bearish candidates per ticker.
- **Stage C catalyst check** via UW headlines + earnings/FDA/guidance
  flags (Finding #4). Degrades gracefully when headlines unavailable.
  Weight 0.10 in final ranking; weights rebalanced to explicit 5-key
  dict: `{trend: 0.30, structure: 0.25, volatility: 0.20, flow: 0.15,
catalyst: 0.10}`.
- **Analysis-only scoping**: `suggested_trade` field removed from
  `TrendCandidate`; replaced with advisory `structure_hint` (defined-risk
  long-side only). Every candidate auto-flagged
  `four_gates_not_applied`. Cross-layer change — Python model + DuckDB
  schema + TypeScript types + web components kept in lockstep
  (Finding #2).

##### Defensive hardening

- SPY pre-cache crash guard: scan no longer aborts when SPY is cold
  and IB is unavailable (Finding #7).
- Staleness check unified: `ta_premarket_prep.classify_tickers`
  delegates to `TAService._is_stale` — audit and scanner now share one
  truth. New `TAService.read_only(conn)` factory encapsulates the audit
  construction (Finding #6).
- Pre-market prep warms the full triple-source scanner universe
  (static + UW flow + IB scanner), not just the S&P 500 static slice
  (Finding #5). Persists to `data/ta_premarket_universe.json` with
  UTC timestamps + honest `source_counts` telemetry. Scanner reuses
  it if <2h old.
- `--audit-only` stays strictly offline — no UW/IB connection attempts.
- UW client cleanup symmetric with IB disconnect; refresh phase wrapped
  in try/finally.

#### Fixed

- `fetch_catalysts` tolerates `earnings_days=None` (UW may not resolve
  earnings date for every ticker) — was raising `TypeError` on the
  `0 <= None` comparison. Regression test added.
- Scanner top-level error handler now logs with `exc_info=True` so
  future regressions surface at stderr instead of being silently
  flattened to a one-line message.

#### Known follow-ups (documented, not blocking)

See `docs/superpowers/plans/2026-04-15-trend-scanner-post-verification-followups.md`:

- `UWClient.get_headlines` not implemented — catalyst score stuck at
  neutral 0.5 until added.
- `TAService.bulk_refresh` 10-min IB pacing sleep drops the socket —
  universe larger than one pacing window cannot be warmed.
- SPY/VIX should be prioritized first in refresh ordering so
  `market_context.regime` isn't `unknown` on partial refreshes.

#### Test coverage

2220 passed / 90 skipped / 0 failed across `scripts/tests/` at the tip
of the branch.

### Previously [0.0.1] - Initial

Project scaffold prior to this changelog.
