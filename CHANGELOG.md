# Changelog

All notable changes to Xenon are documented here. Format loosely based on
[Keep a Changelog](https://keepachangelog.com/) with semver-ish versioning.

## [Unreleased]

## [0.1.0] — 2026-06-02

### Added

- **FUTU NAV curve in the Performance tab (#120).** The FUTU account tab now renders a real backward-walked NAV curve in the existing Performance page — no UI changes, no new components. Persistence layer pulls historical trades + cashflows from Futu OpenD into two new tables (`xenon.futu_trades` + `xenon.futu_cash_flow`), then a FIFO-matched backward walk (with 100× contract multiplier for OCC-format options) populates `xenon.nav_history` rows the existing `/performance?broker=FUTU` route already reads. Verified end-to-end against the operator's live account: 6,189 trades + 424 cashflows over 2024-07-15 → 2026-06-02 produce 688 daily NAV rows anchored at today's `accinfo_query.net_liquidation`.
- **Automatic nightly Futu sync.** New FastAPI lifespan loop runs at 16:30 ET every weekday, calling `xenon-futu-history-sync` end-to-end inside the running xenon-api process. Incremental watermark (`max(filled_at, occurred_at) - 7d`) keeps each tick at ~8 seconds instead of re-walking inception. Disabled in test mode and via `XENON_FUTU_HISTORY_LOOP=0`. Single-day failures log + retry tomorrow; no schedule poisoning.
- **`xenon-futu-history-sync` CLI.** Operator-runnable command that chains the OpenD pulls, persistence UPSERTs, and backward walk. Defaults to incremental; `--since YYYY-MM-DD` forces a deeper rewalk. Resolves FUTU scope from the matched OpenD account per spec §10 (never trusts env vars for FUTU).

### Fixed

- **Futu cashflow type is an open enum.** Prior assumption (`MoneyIn` / `MoneyOut` / …) missed every actual value Futu returns (`Cash Dividend`, `Fund Subscription`, `IPO Subscription`, `Currency Exchange`, `Others`, …). Dropped the `cashflow_type` CHECK and persist verbatim; M5 NAV walk decides which raw types move NAV externally.
- **Bulk insert chunked under Postgres' 32767 bind-param cap.** Live inception backfill of 6,189 deals tripped asyncpg's `InterfaceError: the number of query arguments cannot exceed 32767` on a single `pg_insert.values(...)` call. Now batched at 2000 rows per statement within one transaction.
- **`history_deal_list_query` throttle (10 req/30s).** Multi-window pulls (e.g. 6+ years across 26 paginated windows) tripped Futu's per-endpoint rate limit. Sleeps 3.5s between window calls; single-window pulls pay nothing.

## [0.0.10] — 2026-06-02


### Fixed

- **Realtime relay silently stopped reconnecting after `ECONNREFUSED` (#113).** `classifyIBConnectionError()` built its detector regex by interpolating the configured IB host (e.g. `host.docker.internal:4001`), but Node's `net` module emits the **resolved IP** in the error text (`connect ECONNREFUSED 192.168.5.2:4001`). The regex never matched in Docker deployments, so `scheduleReconnect()` was not called from the `ib.on("error")` handler and the relay sat silent until the process restarted. Symptom: portfolio page rendered positions from Postgres but every live-tick column (`last`, `bid`, `ask`, `close`, `volume`) stayed `—` indefinitely. Fix matches the connect-error code family directly and widens coverage to `ETIMEDOUT`/`EHOSTUNREACH`/`ENETUNREACH`/`ENOTFOUND`/`EAI_AGAIN`/`EADDRNOTAVAIL` — the same silent-death failure mode applied to any of them.
## [0.0.8] — 2026-06-01

### Fixed

- **IB status badge over Tailscale (#110).** `/api/ib/ws-config` now derives the realtime WS URL from the request's `Host` / `X-Forwarded-Host` headers (with `0.0.0.0` / `::` / empty rejected) and respects `X-Forwarded-Proto=https` → `wss://`, instead of echoing back the server's bind address. Previously every request from a remote Tailscale device received `ws://0.0.0.0:8765`, the browser couldn't open it, and the sidebar IB Gateway badge was stuck on "disconnected" even when IB Gateway was fully connected.
- **Connected badge survives realtime relay drops.** `IBStatusContext` + `usePrices` now fall back to polling `/api/health` (15 s) when the WS is offline; if any `ib_pool` role reports `connected: true`, the badge stays green and a new warning banner shows `Live data stream offline — IB Gateway is still connected; prices may be delayed.` rather than telling the user IB itself is down. Distinguishes "relay down" from "broker down" in the UI.

## [0.0.7] — 2026-05-31

### Changed

- **Pure-portfolio pivot.** Xenon is now a broker terminal for options
  portfolio management — IB (primary, live + paper) + Futu (read-only
  positions tab). All signal-generation surfaces removed: scanners (VCG,
  GEX, CRI, GARCH, leap-IV, leap-UW, trend, UW discover, UW analyze),
  flow-analysis page, regime gate + scan loop, CTA / MenthorQ pipelines,
  share-card generators. ChatPanel reframed for portfolio Q&A only.
  See `docs/plans/2026-05-22-pure-portfolio-pivot.md` for the full
  rationale + four-PR breakdown. UW token retained as a historic-data
  source for portfolio_performance + portfolio_report CLIs only.

### Removed

- Python signal layer: `src/xenon/scanners/`, `src/xenon/analysis/`,
  `src/xenon/fetchers/`, `src/xenon/shares/`,
  `src/xenon/services/cta_sync_service.py`, the signal-only
  `api/routes/{regime,uw_analyze,uw_stats}.py` + matching
  `api/services/{regime_*,uw_analyze_*}.py`, the strategy-eval
  `reports/{evaluate,kelly,risk_reversal,free_trade_analyzer,
verify_options_oi,scenario_*}.py`, `clients/{menthorq,massive,
inspect_dashboard,map_*}.py`. ~22k Python LOC.
- server.py inline routes: `/scan`, `/discover`, `/flow-analysis`,
  `/cta/share`, `/regime`, `/regime/scan`, `/regime/share`, `/vcg`,
  `/vcg/scan`, `/vcg/share`, `/gex`, `/gex/scan`, `/gex/share`,
  `/internals/share`, `/internals/skew-history` (18 handlers + 14
  helpers + vcg_cri lifespan + `/health.vcg_cri_loop` field).
- Web pages: `/cta`, `/discover`, `/flow-analysis`, `/internals`,
  `/kit`, `/regime`, `/scanner`, `/uw-analyze`. ~10k web LOC.
- Web components: `CtaPage`, `GexPanel`, `RegimePanel`,
  `InternalsPanel`, `CriHistoryChart`, `SharePnlButton`,
  `ShareReportModal`, `VcgPanel`, `RegimeRelationshipView`, etc.
- Sidebar UW telemetry footer + `useUwStats` / `useUwStatsHistory` hooks.
- pyproject.toml: ~31 CLI entry points (`xenon-fetch-*`, `xenon-*-scan`,
  `xenon-evaluate`, `xenon-kelly`, `xenon-scenario-*`, `xenon-leap-*`,
  `xenon-trend-scan`, `xenon-garch`, `xenon-uw-*`, `xenon-discover*`,
  `xenon-generate-*-share`, `xenon-cta-sync-service`,
  `xenon-repair-cri-rvol`). Dropped `playwright` + `yfinance` deps.
- Launchd plists: `com.xenon.{cri-scan,cta-sync,data-refresh}.plist`
  and companion shell scripts under `scripts/services/`.
- Env vars: `MENTHORQ_USER` / `MENTHORQ_PASS` / `MASSIVE_API_KEY`
  (clients gone), `CEREBRAS_API_KEY` (always dead).

### Deferred (separate follow-up)

- Postgres tables `uw_analyze_snapshots` (+ child tables), `vcg_series`,
  `gex_snapshots`, `scan_results`, `regime_state_view`,
  `regime_overrides`, `uw_flow_events` are intentionally left in place.
  A separate Alembic downgrade will drop them once we've confirmed
  zero readers in the running services.

## [0.0.6] — 2026-05-26

### Documentation

- **Clarify Docker-to-IB Gateway routing for Mac mini deploys.** `docs/runbooks/remote-deploy.md` now distinguishes co-located IB Gateway (`host.docker.internal`) from IB running on another LAN/Tailscale host or behind a localhost-only SSH tunnel. The bootstrap step no longer rewrites `IB_GATEWAY_HOST` unconditionally, and `docker-compose.yml` comments now describe the same topology boundary.

## [0.0.5] — 2026-05-05

### Fixed

- **Forward `UW_TOKEN` into the FastAPI container (#99).** `docker-compose.yml` api service now also loads `./web/.env` (after `./.env`, marked `required: false`). Previously the container booted without `UW_TOKEN` because the credential lives in `web/.env` per CLAUDE.md and the api Dockerfile never ships the `web/` tree, so the in-process `load_dotenv(web/.env)` at `server.py:81-82` silently no-opped — and every UW-backed endpoint failed with the existing `"UW_TOKEN not set"` warning. Side benefit: same channel now plumbs `ANTHROPIC_API_KEY` through to `menthorq_client`. Regression test in `scripts/tests/test_docker_compose_env_plumbing.py` pins env_file order + optional flag.
- **`scripts/infra/dev.sh` paper-mode now works off-LAN (#98).** Per-mode IB Gateway host resolution: `paper` always pins `127.0.0.1:4002`, `live` honors `IB_GATEWAY_HOST` from `.env`. `.env.example` documents the new var.

### Changed

- **GHCR image tags standardized on `X.Y.Z` (no `v` prefix) (#97).** Aligns with Docker convention; the matching tag in `release.yml::ghcr-push` was the previous odd-one-out.

### Documentation

- Stage C auto-deploy planning handover (#88), backlog status updates (#93), GHCR per-package ACL prerequisite (#94), `XENON_BROKER_ACCOUNT` runbook + Clerk dev-limit correction (#92), and archival of completed plans + top-level docs (#95, #96).

## [0.0.4] — 2026-05-04

### Added

- **Default-private route gating with explicit public allowlist (#90).** Inverts `web/middleware.ts` from "gate everything except sign-in/sign-up/api" to default-private with an explicit `PUBLIC_ROUTES` array of scanner / market-data surfaces (`/scanner`, `/discover`, `/regime`, `/flow-analysis`, `/uw-analyze`, `/cta`, `/kit`). Trading workspace (`/`), portfolio, orders, journal, internals, dashboard, performance, and `/[ticker]` stay private. New routes inherit auth automatically — opt out by adding to `PUBLIC_ROUTES`. Fail-closed default protects against scattered per-flow gating regressions called out in `project_universal_auth_gating` memory. New unit test `web/tests/middleware-route-gating.test.ts` pins 24 path classifications including the "unlisted route is private" invariant.
- **Mac mini Docker deploy runbook (#87).** New `docs/runbooks/remote-deploy.md` documents topology, first-time bootstrap, standard release flow, Colima auto-start via `brew services`, rollback, logs/diagnostics, and a reference compose template. `docs/runbooks/mac-mini.md` now points at it as the authoritative source.
- **Stage C auto-deploy planning handover (#88).** `docs/handovers/2026-05-04-stage-c-auto-deploy.md` — three-item plan covering release verify, build-args, and Watchtower on the mini.

### Fixed

- **`release.yml::verify` now provisions Postgres (#89).** Adds `postgres:16-alpine` service container, schema bootstrap (`xenon`, `events`), and `alembic upgrade head` before `uv run pytest`. Mirrors `ci.yml::python-tests` so the tag-trigger verify matches what master CI proves green. Without it, v0.0.3 verify hit 282 connection-refused errors and blocked `ghcr-push`; v0.0.4 is the first tag where verify is actually expected to pass clean.
- **Clerk publishable key now baked into web image (#89).** `release.yml::ghcr-push` passes `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` as a `docker/build-push-action` build-arg via matrix conditional (web image only). `NEXT_PUBLIC_*` vars must be present at `next build` time so they're inlined into the client bundle; without it the production web image shipped with an undefined Clerk key and round-tripped to localhost on auth flows. Requires GHA repo secret `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` to be set (added 2026-05-04).

## [0.0.3] — 2026-05-04

### Added

- **Phase 1 Stage A — containerization (#85).** Four production deploy images (`api`, `web`, `realtime`, `migrator`) plus a single `docker-compose.yml` at repo root that runs the long-running stack (api + web + realtime); migrator is gated behind `profiles: ["migrate"]` so `compose up` never auto-runs alembic. Web image uses Next.js standalone output with `NEXT_PUBLIC_*` build-args inlined at build time; realtime image carries `scripts/lib/` for the relay's `lru-cache` import. Tag-triggered `ghcr-push` matrix job in `release.yml` builds and publishes all four images to `ghcr.io/<owner>/xenon-{api,web,realtime,migrator}` (linux/arm64) with `:vX.Y.Z` + `:latest` tags. `host.docker.internal` reaches host-native Postgres + IB Gateway + Futu OpenD; `XENON_API_URL=http://api:8321` wires server-side Next fetches to FastAPI inside the compose network.
- **VCG-CRI regime gate Phase 3 — order entry wiring (#78).** `RegimeGate` now intercepts every order placement and modify/cancel path, scoped via `AccountScope`. Composite 4-tuple FK on `regime_overrides` (`submission_id, broker, account_env, broker_account`) prevents scope drift between override rows and parent submissions. New audit table records every blocked/permitted decision with the resolved (vcg_tier, cri_tier) tuple at decision time.
- **VCG-CRI rewiring Phase 0/1/2/4 (#68).** CRI persistence (`cri_series`), `pg_try_advisory_lock` helper, web `/api/regime` rewrite, audit doc. The eight tribunal corrections from the v2 design are folded inline.
- **Per-mode IB Gateway host routing in `dev.sh` (#83).** `paper` resolves to `127.0.0.1:4002` (always local), `live` resolves to whatever `IB_GATEWAY_HOST` from `.env` points at (typically the remote production server) on port `4001`. `IB_GATEWAY_HOST` and `IB_GATEWAY_PORT` are exported so child processes (uvicorn, Next, the Node IB realtime relay) inherit the resolved values.

### Changed

- **`ib_insync` → `ib_async` (#81).** `ib_insync` was last released July 2023 (0.9.86) and is unmaintained; newer Gateway versions (10.30+) tightened the API handshake. Migrated to the actively-maintained community fork `ib_async==2.1.0`. Package-name-only swap across 34 source/test files; no code-shape changes. 132 ib-related unit tests pass against the new dep.
- **PG migration clean cutoff — drop `data/*.json` runtime reads (#84).** Strips silent JSON fallbacks from three UW services and tightens the order-path JSON-fallback allowlist to zero. Conftest gains `scope_fixture`, `pg_test_engine`, and an offline-tolerant PG truncate that caches unreachability so the test suite no longer dies on a 5-minute psycopg timeout when the LAN is unreachable. UW analyze cache hardened: closed-market gate covers user-initiated paths, write failures emit observability events.
- **`output: 'standalone'` in `web/next.config.mjs`.** Required for the slim web runtime image; preserves the existing `outputFileTracingRoot` pointing at the repo root.
- **Multi-service Postgres design (#82).** Same cluster, role-isolated schemas (`xenon`, `apex`, `events`); three DB names (`core`, `core_dev`, `core_test`) for prod/dev/test separation. `xenon_app/xenon_dev`, `apex_app/apex_app`, admin `moremeds/moremeds`. Documented in `docs/architecture/production-database-strategy.md`.

### Fixed

- **Combo SELL blanket bypass closed (#79).** `_is_regime_gate_risk_reducing_exit` no longer trusts a SELL BAG envelope. Combo SELL bypass now requires every per-leg inverse to exist in the portfolio with sufficient contracts via `preflight.combo_close_covered_by_portfolio`. `preflight.combo_uncovered_short_call_ratio` + `evaluate_combo` drop their `action == "SELL" → accept` short-circuit. Per-leg ratio analysis is the trustworthy check for naked-short risk; the envelope action is untrusted.
- **Stale portfolio snapshot allowed bypass (#79).** SELL-to-close bypass now calls `_portfolio_snapshot_stale_response`. A snapshot beyond the freshness threshold (300s open / 1800s closed) refuses the bypass and lets RegimeGate block as new exposure.
- **BAG quantity-increase modify outside NORMAL tier (#80).** `_run_modify_regime_gate` now blocks modify operations that increase exposure when the regime tier is anything but NORMAL.

### Removed

- **Dead `incremental_sync` + post-hoc reliability plan (#73).** Code removed; tests rewired to the live sync path.
- **Vestigial `db_path` parameter in `orders_store` (#75).** All 11 functions stripped of the DuckDB-era param. Tracked in memory `feedback_orders_store_no_db_path.md` so it never returns.

### Documentation

- **Tick-stub by-design doc (#76).** `_lookup_min_tick_via_pool` returns 0.01 for every contract on purpose; IB enforces the real rule and code 110 maps to LIMIT_OFF_TICK. Captured in code comments + memory.
- **Order-stack end-to-end walkthrough (#83).** New `docs/architecture/order-stack-end-to-end.md` traces a single order from web click through FastAPI to IB Gateway and back through the activity poller.
- **Backlog hygiene (#74, plus chronological cross-reference of inbox items vs commits).**

## [0.0.2] — 2026-04-30

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
