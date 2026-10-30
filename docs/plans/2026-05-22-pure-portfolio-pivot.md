# Pure Portfolio Pivot — Removal Plan

**Date:** 2026-05-22
**Author:** chenxi (with Claude analysis)
**Status:** DRAFT v2 — post-adversarial-review (confidence ~90%)

## Revisions from adversarial review (2026-05-22)

A grep/read pass against the actual code surfaced four load-bearing errors in v1 of this plan. Fixed inline below; flagged here so future readers don't trust v1 quotes pulled out of context.

1. **UW_TOKEN must stay.** `reports/portfolio_performance.py` and `reports/portfolio_report.py` use `UWClient` for stock OHLC + option historic prices, with a **Yahoo fallback that root `CLAUDE.md` forbids** ("Never use Yahoo Finance"). Dropping `UW_TOKEN` either breaks portfolio reports or silently triggers a banned fallback path. Resolution: UW becomes a _historic data source_ (priority #2 per root CLAUDE.md), not a signal source. `clients/uw_client.py`, `utils/uw_api_stats.py`, `utils/uw_cache.py` all stay. `xenon.db.schema.uw_api_stats` table stays.

2. **`api/server.py` surgery is much bigger than "drop a few `include_router` calls."** The file has 40 `@app.<method>` decorators — most of the signal surface is **inline in server.py**, not in `routes/`. Verified deletions: `/scan` (POST+GET), `/discover` (POST+GET), `/flow-analysis` (POST+GET), `/cta/share`, `/regime`, `/regime/scan`, `/regime/share`, `/vcg`, `/vcg/scan`, `/vcg/share`, `/gex`, `/gex/scan`, `/gex/share`, `/internals/share`, `/internals/skew-history`. Plus helpers `_resolve_expiry_candidates` and `_fetch_risk_reversal_history` (~110 LOC, both UW-dependent, both only called by skew/risk-reversal). Plus the `vcg_cri_loop` lifespan task at line 670 and its `app.state.vcg_cri_task` field. Plus the `_vcg_cri_loop_health()` field in `/health`. Plus top-level imports of `regime_router`, `uw_analyze_router`, `regime_gate`, `regime_state`, `UWAPIError`/`UWClient`/`UWNotFoundError`, and several inline `from xenon.api.services.uw_analyze_*` imports inside removed handlers.

3. **`/options/chain` and `/options/expirations` ARE safe for order entry.** Both call `xenon-ib-option-chain` as a subprocess (pure IB). The UW-using `_resolve_expiry_candidates` is only consumed by the skew-history / risk-reversal handlers (which are removed). Order entry chain selection (OrderTab + OptionsChainTab + `useChainPrefetch`) is unaffected.

4. **Combo wizard is the active order entry flow** — verified. Used by `web/components/ticker-detail/OrderTab.tsx`, `OptionsChainTab.tsx`, `web/lib/useWizardSession.ts`. The whole `/api/wizard/*` Next route tree (plan + sessions + stream + submit + reprice + abort + protect) and the `combo_wizard/` Python module are KEEP.

Smaller corrections:

- **`CEREBRAS_API_KEY`** — no usage found anywhere in the codebase. Already dead; remove from `web/.env.example`.
- **`MENTHORQ_USER`/`MENTHORQ_PASS`** — only used by `clients/menthorq_client.py` and `fetchers/fetch_menthorq_cta.py`, both REMOVE. Safe to drop env vars.
- **`MASSIVE_API_KEY`** — only used by `clients/massive_client.py`. Confirm `massive_client` is signal-side only before dropping the env var (Q-prep below).
- **`EXA_API_KEY`** — used by `web/app/api/ticker/info/route.ts` (research enrichment on ticker pages). Decision deferred to Q-prep — could stay if you want ticker-page research enrichment.
- **AttributionPanel uses `data.kelly_calibration`** — `reports/portfolio_attribution.py` produces these Kelly-calibration fields. The Kelly _concept_ survives in **portfolio attribution context** (per-position sizing report); only the standalone `reports/kelly.py` strategy CLI is removed.
- **No external callers** of removed CLI entries in `~/projects/`. Grep was empty.
- **WorkspaceSections.tsx** has no signal-component imports (only `MetricCard`, `SourceBadge`, `TableSkeleton`). PR-B doesn't need to touch it.
- **`/api/pi/route.ts`** spawns `.venv/bin/xenon-evaluate` (assistant tool surface). Removing `xenon-evaluate` breaks this route — already in REMOVE list, just calling out the linkage.
- **`/uw-stats` FastAPI endpoint** — keep or drop is a coin flip. The Sidebar UW telemetry strip + the two `useUwStats*` hooks + `web/app/api/uw-stats/*` Next routes all go. Recommendation: also drop the FastAPI `/uw-stats` routes since the UI is gone and operator-only telemetry can be read from the underlying Postgres table directly.

## Goal

Recast xenon from a **market-structure / signal-detection system** into a focused **broker terminal**: place orders, manage positions, monitor portfolio risk, reconcile fills, and audit broker activity for IB (primary) + Futu (read-only).

Everything that exists only to generate, score, rank, or display a trading _signal_ (UW flow, dark pool, regime, GEX, VCG, CRI, CTA, trend, leap-IV, garch) is out of scope and removable.

## Scope decisions (confirmed 2026-05-22)

1. **Futu stays** — read-only positions snapshot as a second account tab.
2. **ChatPanel stays** — but with the system prompt rewritten to portfolio-Q&A only. It has no tool calls today; only the system prompt and the fallback replies need a content rewrite.
3. **Postgres tables stay in place** — code stops reading/writing the signal tables (`uw_analyze_snapshots`, `vcg_series`, `gex_snapshots`, `scan_results`, `regime_state_view`, etc.). A separate migration PR can drop tables later once we confirm no readers remain. Live order-audit tables are untouched (`order_submissions`, `order_events`, `order_fills`, `position_protection`, `position_close_claims`, `events.outbox`).

## Identity reset

Today's `CLAUDE.md` opens with _"Xenon — market structure reconstruction system. Surfaces convex opportunities from dark pool/OTC flow… Flow signal or nothing."_ and lists Four Gates (Convexity, Edge, Risk Mgmt, No Naked Shorts).

Post-pivot identity:

> **Xenon** — broker terminal for options portfolio management. Live IB + read-only Futu integration. Places orders, tracks fills, enforces position-close rules, surfaces P&L and Greeks attribution. No signal generation, no scanner output — bring your own thesis.

What survives from the old gates: **Gate 4 (No Naked Shorts)** stays — it's a structural guard on the order path, enforced by `naked_short_audit` + the combo wizard. Gates 1–3 (convexity / edge / Kelly sizing) describe a strategy and are no longer the system's responsibility.

## KEEP — the "broker terminal" core

### Python (`src/xenon/`)

| Path                                      | Reason                                                                                             |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `execution/` (all of it)                  | IB place/manage/cancel/reconcile, Futu sync, account scope, naked-short audit, combo wizard        |
| `trade_blotter/`                          | IB Flex query — historical fills + execution audit                                                 |
| `cli/position_rules.py`                   | Position-close rule CLI                                                                            |
| `monitor_daemon/` (all of it)             | Position-rules enforcement, fill monitor, preset rebalance, out-of-band sweep, flex-token check    |
| `jobs/flex_divergence_check.py`           | Audit job — compares Flex vs DB state                                                              |
| `api/server.py`                           | (pruned — drop signal routers)                                                                     |
| `api/routes/orders.py`                    | Order placement, modify, cancel                                                                    |
| `api/routes/position_rules.py`            | Position-close rule HTTP layer                                                                     |
| `api/routes/historical.py`                | Historical positions / fills                                                                       |
| `api/routes/journal.py`                   | Trade journal                                                                                      |
| `api/routes/trades.py`                    | Trade list                                                                                         |
| `api/routes/wizard.py`                    | Combo order wizard (plan/submit/reprice/abort/protect)                                             |
| `api/services/advisory_lock.py`           | Postgres advisory locks                                                                            |
| `api/services/ib_activity_mirror.py`      | Mirror IB orders into Postgres                                                                     |
| `api/services/journal_auto_import.py`     | Auto-import fills into journal                                                                     |
| `api/services/position_rules_*.py`        | Position-rule cancel + health                                                                      |
| `reports/portfolio_report.py`             | Portfolio snapshot report                                                                          |
| `reports/portfolio_attribution.py`        | P&L attribution                                                                                    |
| `reports/portfolio_performance.py`        | Performance tracking                                                                               |
| `reports/performance_explainer_report.py` | Performance attribution narrative (CONFIRM portfolio-bound, not strategy-eval)                     |
| `reports/blotter.py`                      | Tiny stub for blotter                                                                              |
| `db/` (all migrations)                    | Migrations retained; signal tables left in place                                                   |
| `clients/ib_client.py` + IB-related utils | IB Gateway wrapper                                                                                 |
| `utils/`                                  | Core: `account_scope` helpers, market hours, presets, auth. Drop UW-specific helpers (see Remove). |

### Web (`web/`)

| Path                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Reason                                           |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `app/portfolio/`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | Positions table, account tabs                    |
| `app/orders/`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Order entry + active orders                      |
| `app/journal/`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Trade journal                                    |
| `app/performance/`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | P&L over time, attribution                       |
| `app/[ticker]/`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Per-ticker drilldown (positions + quote + chain) |
| `app/dashboard/`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | Top-level overview                               |
| `app/api/orders/`, `portfolio/`, `journal/`, `futu/`, `ib/`, `health/`, `blotter/`, `attribution/`, `performance/`, `position-rules/`, `ticker/`, `previous-close/`, `prices/`, `wizard/`                                                                                                                                                                                                                                                                                                                                         | Backing API routes                               |
| `app/api/assistant/`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Chat — kept with system prompt rewrite           |
| Components: `PositionTable`, `OrderTab`, `ModifyOrderModal`, `PositionOrderModal`, `AttributionPanel`, `PerformancePanel`, `AccountTabBar`, `FillsModal`, `CancelOrderDialog`, `ExposureBreakdownModal`, `PnlBreakdownModal`, `PortfolioByStructure`, `InstrumentDetailModal`, `TickerDetailContent`, `MetricCards`, `Header`, `Sidebar` (pruned), `Modal`, `MarkdownRenderer`, `InfoTooltip`, `ChatPanel`, `ConnectionBanner`, `FlexTokenBanner`, `OrderErrorBanner`, `QuoteTelemetry`, `SourcePill`, `TableSearch`, `Providers` | Core portfolio UI                                |
| `components/portfolio/*`, `components/ticker-detail/*`, `components/ui/*`, `components/charts/PriceChart.tsx`                                                                                                                                                                                                                                                                                                                                                                                                                     | Subcomponents in scope                           |
| `lib/order/`, `lib/futu*`, `lib/exposureBreakdown.ts`, `lib/optionsChainUtils.ts`, `lib/xenonApi.ts`, `lib/types.ts`, `lib/data.ts` (pruned nav), price helpers, `lib/auth/`, `lib/positionRules/`                                                                                                                                                                                                                                                                                                                                | Core helpers                                     |
| `playwright.config.ts`, `vitest.config.ts`, `tests/` (relevant only)                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Test infra                                       |
| `middleware.ts`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Route gating (pruned for removed routes)         |

## REMOVE — signal & analytics layer

### Python — drop entire directories

```
src/xenon/scanners/         # 11,106 LOC — vcg, gex, cri, garch, leap_iv, leap_uw, trend, uw, discover*, repair_cri_rvol_cache
src/xenon/analysis/         # 1,544 LOC — dark_pool_summary, options_flow_summary, gex, vrp, ticker_data, gates, scoring, benchmark, models
src/xenon/fetchers/         # 4,876 LOC — flow, ticker, options, oi, analyst, news, menthorq_*, x_watchlist, x_xai
src/xenon/shares/           # 2,821 LOC — cta/gex/regime/vcg share-card generators
src/xenon/services/cta_sync_service.py
```

### Python — drop individual files

```
src/xenon/api/routes/uw_analyze.py        # already gone in PR #103
src/xenon/api/routes/regime.py
src/xenon/api/routes/uw_stats.py          # see also: drop Sidebar UW telemetry
src/xenon/api/services/uw_analyze_cache.py
src/xenon/api/services/uw_analyze_candidates.py
src/xenon/api/services/uw_analyze_daily_job.py
src/xenon/api/services/uw_analyze_diff.py
src/xenon/api/services/uw_analyze_flow_tracker.py
src/xenon/api/services/uw_analyze_oi_snapshots.py
src/xenon/api/services/uw_analyze_oi_tracker.py
src/xenon/api/services/uw_analyze_portfolio_bias.py
src/xenon/api/services/regime_gate.py
src/xenon/api/services/regime_state.py
src/xenon/reports/evaluate.py
src/xenon/reports/kelly.py                # standalone Kelly CLI — Kelly fields in portfolio_attribution.py stay
src/xenon/reports/risk_reversal.py
src/xenon/reports/free_trade_analyzer.py
src/xenon/reports/verify_options_oi.py
src/xenon/reports/scenario_analysis.py
src/xenon/reports/scenario_report.py
```

**DO NOT REMOVE (revised from v1):**

```
src/xenon/utils/uw_api_stats.py     # UWClient depends on it; UW is data-source role
src/xenon/utils/uw_cache.py         # ditto
src/xenon/clients/uw_client.py      # used by portfolio_performance + portfolio_report
src/xenon/clients/menthorq_client.py  # Q-prep: confirm no kept caller before deletion
src/xenon/clients/massive_client.py   # Q-prep: confirm no kept caller before deletion
```

### `api/server.py` surgery — inline route deletions

Mechanical but non-trivial. Delete the following `@app.<method>` handlers and any helpers used only by them:

```
/scan         (GET + POST)        ~lines 1800-1815
/discover     (GET + POST)        ~lines 1815-1832
/flow-analysis (GET + POST)       ~lines 1832-1869
/cta/share                        ~line 3456
/regime                           ~line 3465
/regime/scan                      ~line 3474
/regime/share                     ~line 3819
/vcg                              ~line 3503
/vcg/scan                         ~line 3550
/vcg/share                        ~line 3721
/gex                              ~line 3784
/gex/scan                         ~line 3793
/gex/share                        ~line 3737
/internals/share                  ~line 3828
/internals/skew-history           ~line 3837
```

Helpers to delete with them (only used by the above):

```
_resolve_expiry_candidates()      ~line 1328  (UW-dependent expiry helper)
_fetch_risk_reversal_history()    ~line 1368  (UW-dependent skew helper)
_compose_expiry_candidates()      ~line 1348
_needs_deeper_backfill()          ~line 1320 (verify)
_vcg_cri_loop_health()            ~line 1570
```

Top-level imports to delete:

```python
from xenon.api.routes.regime import router as regime_router
from xenon.api.routes.uw_analyze import router as uw_analyze_router
from xenon.api.routes.uw_stats import router as uw_stats_router
from xenon.api.services.regime_gate import ...
from xenon.api.services.regime_state import get_regime_state_for_scope
from xenon.clients.uw_client import UWAPIError, UWClient, UWNotFoundError  # only if no other server.py site still needs it
```

Plus the corresponding `app.include_router(...)` calls.

Lifespan cleanup:

- Remove the `XENON_VCG_CRI_LOOP` block (~line 670 in `lifespan()`) and the `app.state.vcg_cri_task` field.
- Remove the `vcg_cri_loop` field from the `/health` response.

`/uw-stats` decision (revised): drop the FastAPI endpoint along with the router, since the UI consumer is gone. Operator visibility into UW call telemetry can still come from querying the `xenon.uw_api_stats` Postgres table directly.

### Python — drop CLI entries from `pyproject.toml`

Drop ~30 of the 50+ entries. Keep only:

```
xenon-api
xenon-blotter
xenon-blotter-history
xenon-market-hours
xenon-presets
xenon-monitor-daemon
xenon-position-rules
xenon-preset-rebalance
xenon-ib-execute
xenon-ib-place-order
xenon-ib-order-manage
xenon-ib-orders
xenon-ib-option-chain
xenon-ib-reconcile
xenon-ib-sync
xenon-naked-short-audit
xenon-futu-sync
xenon-portfolio-report
xenon-portfolio-attrib
xenon-portfolio-perf
xenon-perf-explainer   # confirm portfolio-bound
```

Drop everything else (`xenon-fetch-*`, `xenon-*-scan`, `xenon-discover*`, `xenon-generate-*-share`, `xenon-evaluate`, `xenon-kelly`, `xenon-risk-reversal`, `xenon-free-trade-analyzer`, `xenon-scenario*`, `xenon-cta-sync-service`, `xenon-leap-*`, `xenon-garch`, `xenon-uw-*`, `xenon-trend-scan`, `xenon-repair-cri-rvol`).

### Web — drop pages + API routes + components

**Pages:**

```
web/app/cta/
web/app/discover/
web/app/flow-analysis/
web/app/internals/
web/app/kit/
web/app/regime/
web/app/scanner/
web/app/uw-analyze/   # already gone in PR #103
```

**Next API proxies:**

```
web/app/api/assistant/      # KEEP — only system prompt rewrite needed
web/app/api/flex-token/     # KEEP — FlexTokenBanner depends on it (verified)
web/app/api/options/        # KEEP — OrderTab + OptionsChainTab depend on it (verified IB-only)
web/app/api/ticker/         # KEEP — ticker page (info subroute uses EXA_API_KEY — see Q-prep)
web/app/api/cta/
web/app/api/discover/
web/app/api/flow-analysis/
web/app/api/gex/
web/app/api/internals/
web/app/api/menthorq/
web/app/api/pi/             # spawns xenon-evaluate which is removed; route dies with the CLI
web/app/api/regime/
web/app/api/scanner/
web/app/api/share/
web/app/api/uw-analyze/     # already gone in PR #103
web/app/api/uw-stats/
web/app/api/vcg/
```

**Components:**

```
CtaPage.tsx
CtaTables.tsx
CtaBriefing.tsx
SortableCtaTable.tsx
CriHistoryChart.tsx
GexPanel.tsx
InternalsPanel.tsx
InternalsSkewChart.tsx
RegimePanel.tsx
RegimeBlockModal.tsx
RegimeStrip.tsx
RegimeTierStrip.tsx
RegimeRelationshipView.tsx
SharePnlButton.tsx
ShareReportModal.tsx
```

**Sidebar prune** (`web/lib/data.ts` `navItems`):

Drop `Flow Analysis`, `UW Analysis`, `Scanner`, `Discover`, `Regime`, `CTA`, `Internals`. Keep `Dashboard`, `Portfolio`, `Performance`, `Orders`, `Journal`.

Also strip the UW telemetry footer from `Sidebar.tsx` (verified lines 95–149: from `<div className="sidebar-footer-divider" />` to the `daily.requests_5xx > 0` errors row), drop both `useUwStats` and `useUwStatsHistory` hooks (`web/lib/useUwStats.ts`, `web/lib/useUwStatsHistory.ts`), and remove the `.uw-stats-errors` CSS class from `web/app/globals.css`.

**Other web/lib cleanups:**

- `web/lib/chat.ts` — strip `/evaluate` command mapping; rewrite the command examples for the portfolio terminal.
- `web/lib/data.ts` — strip the chat example prompts (`"evaluate brze"`, `"evaluate msft"`, `"evaluate igv"`).
- `web/lib/regime*.ts`, `web/lib/useRegime*.ts`, signal-only helpers — sweep with a grep for imports from removed pages.

### Env vars to drop

`web/.env`:

- `CEREBRAS_API_KEY` — verified dead (no callers anywhere)
- (revised) `EXA_API_KEY` — only `web/app/api/ticker/info/route.ts` uses it. **Decision deferred to Q-prep.** If you want web-research enrichment on ticker pages, keep both. Otherwise drop the route + env var together.

**KEEP (revised from v1):**

- `UW_TOKEN` — `reports/portfolio_performance.py` and `reports/portfolio_report.py` use UWClient for OHLC + option historic. Without UW the only alternative is the Yahoo fallback CLAUDE.md forbids.
- `ANTHROPIC_API_KEY` — chat panel.
- Clerk keys — auth.

Root `.env`:

- Keep IB, Postgres, Clerk JWKS/issuer, IB Flex token.
- Drop `MENTHORQ_USER` / `MENTHORQ_PASS` (only used by removed `menthorq_client.py` + `fetch_menthorq_cta.py`).
- **Q-prep:** confirm `MASSIVE_API_KEY` has no kept caller (only `clients/massive_client.py` references it; verify `massive_client` isn't imported by any KEPT module before dropping).

## ChatPanel system-prompt rewrite

**Current** (`web/app/api/assistant/route.ts` SYSTEM_PROMPT):

> "You are Xenon's trading operations assistant. You analyze institutional flow, portfolio risk, and trade structure. Always respond in short, decisive blocks using signal, structure, kelly logic, and final decision. If confidence is low, explicitly state uncertainty and recommend next command or additional data."

**Proposed:**

> "You are Xenon's portfolio operations assistant. You help the user understand their open positions, current orders, Greeks exposure, and recent fills. Respond in short, factual blocks. Reference live position data the user has provided rather than inventing it. If a question depends on data you can't see, say so and tell the user which page or panel to check. Never recommend trades, signals, or market direction — Xenon does not generate trade ideas."

Also rewrite the `fallbackReply()` hardcoded strings — they reference BRZE/RR flow analysis and should be replaced with generic portfolio prompts.

## PR sequencing

### PR-A — Python signal layer (no UI changes)

Bigger than v1 implied — `server.py` has 12+ inline routes plus helpers to delete, not just routers to unwire.

- Delete `src/xenon/scanners/`, `analysis/`, `fetchers/`, `shares/`, `services/cta_sync_service.py`
- Delete signal-only `api/routes/` (regime.py, uw_analyze.py if not yet, uw_stats.py)
- Delete signal-only `api/services/` (regime*gate.py, regime_state.py, all 8 uw_analyze*\*.py)
- Delete signal-only `reports/` (evaluate, kelly, risk*reversal, free_trade_analyzer, scenario*\*, verify_options_oi)
- **`api/server.py` surgery** (per the inline-deletion section above): drop 15 inline `@app.<method>` handlers, 4-5 helper functions, vcg_cri_loop lifespan block + state field, `/health` `vcg_cri_loop` field, top-level imports of regime/uw_analyze/uw_stats routers + services + UWClient (only if no remaining server.py site uses it)
- Drop `clients/menthorq_client.py` + `clients/massive_client.py` once Q-prep confirms no kept caller
- Prune `pyproject.toml` `[project.scripts]` (drop ~30 entries)
- Prune `scripts/checks/no_json_fallback_on_order_path.py::_ALLOWLIST` for dead paths
- Delete corresponding `scripts/tests/test_*.py` for removed modules (do not leave xfails)
- Delete `data/gex.json` (only signal artifact). Keep `flex_token_config.json`, `portfolio.json`, `reconciliation.json`, `daemon_state.json`, `trade_log.json`, `price_history_cache/`, `service_health/`, `locks/`
- Verify `uv run pytest` + `uv run python scripts/infra/dev/run_pytest_affected.py`
- Verify FastAPI boots: `curl localhost:8321/health` after restart

### PR-B — Web signal pages (UI)

- Delete `web/app/{cta,discover,flow-analysis,internals,kit,regime,scanner}/`
- Delete corresponding `web/app/api/{cta,discover,flow-analysis,gex,internals,menthorq,pi,regime,scanner,share,uw-stats,vcg}/`
- Delete signal-only components (full list above)
- Prune `web/lib/data.ts` navItems + chat example prompts (`"evaluate brze"`, etc.)
- Strip `/evaluate` command from `web/lib/chat.ts`
- Strip UW telemetry from `Sidebar.tsx` (lines 95–149) + delete `useUwStats.ts` + `useUwStatsHistory.ts` + `.uw-stats-errors` CSS class
- Sweep `web/lib/regime*.ts` and other signal-only helpers
- Update `middleware.ts` to drop gating for removed routes
- Add `web/tests/{nav,middleware-route-gating}.test.ts` cases pinning the removed routes 404
- Run `cd web && npm run typecheck && npm test && npx playwright test`

### PR-C — Chat panel rewrite + env cleanup

- Update `web/app/api/assistant/route.ts` SYSTEM_PROMPT + fallback replies (no more BRZE/RR examples)
- Drop `CEREBRAS_API_KEY` (verified dead), `MENTHORQ_USER`/`MENTHORQ_PASS` (menthorq client/fetcher gone) from `.env.example` and docs
- Conditionally drop `MASSIVE_API_KEY` (after Q-prep confirms no kept caller of `massive_client.py`)
- Conditionally drop `EXA_API_KEY` + `web/app/api/ticker/info/route.ts` (only if you decide against research enrichment on ticker pages)
- **KEEP** `UW_TOKEN` and `ANTHROPIC_API_KEY` (load-bearing for portfolio reports and chat respectively)
- Update root `CLAUDE.md` Credentials table to reflect the trimmed set

### PR-D — Identity reset + docs

- Rewrite root `CLAUDE.md` Identity + Four Gates section (keep only naked-short guard)
- Rewrite `src/xenon/CLAUDE.md`, `web/CLAUDE.md` — strip signal references, simplify Mandatory Rules, drop scanner hierarchy section
- Move `docs/handovers/*` and signal-era plans to `docs/archive/`
- Update `brand/CLAUDE.md` if brand identity needs softening (Xenon visual identity probably stays; tagline copy changes)
- Update `CHANGELOG.md` under `[Unreleased]` with a single coherent entry describing the pivot
- Drop `docs/todo-backlog.md` items that reference removed surfaces (mark RESOLVED-BY-REMOVAL)

### PR-E (later, separate) — Migration cleanup

- Drop unused Postgres tables: `uw_analyze_snapshots`, `vcg_series`, `gex_snapshots`, `scan_results`, `regime_state_view`, regime overrides, UW flow events, scan cooldown tables, etc.
- Drop CI guard tests that asserted no JSON fallback into these tables (they become moot)
- Update `docs/architecture/production-database-strategy.md`

## Risks

1. **PR #103 (uw-analyze removal) must land first.** It already unwinds the `flow-analysis` dependency on `uw_analyze_portfolio_bias`. Build on top of `master` after merge.
2. **`server.py` is the highest-risk file** — 15 inline route deletions + 4 helper deletions + lifespan changes in one PR. Stage the diff so each handler removal is a separate hunk; verify FastAPI boots cleanly between groups locally before pushing. (`uv run xenon-api` + `curl localhost:8321/health`).
3. **UW data dep is non-obvious.** `portfolio_performance.py` and `portfolio_report.py` look like pure portfolio code but reach for UWClient. If you ever do drop UW_TOKEN, plan a refactor first (IB historical bars as substitute).
4. **Order-path guard allowlists** (`scripts/checks/no_json_fallback_on_order_path.py::_ALLOWLIST`, `scripts/checks/order_path_caller_allowlist.py`) — entries referencing removed files become dead. Prune as part of PR-A.
5. **`docs/reference/order-path-incident-history.md`** stays — it's still the canonical incident log for the surviving order path.
6. **Tests for removed code:** delete in the same PR as the code, not later. Don't leave hanging xfails.
7. **`xenon` namespace import paths** — verified empty for `xenon-*-scan`, `xenon-uw-*`, `xenon-fetch-*` from other `~/projects/` (grep was empty). One less worry.
8. **Brand identity copy in UI** — Sidebar header still says "Xenon"; the brand mark stays. Only taglines / page heroes need rewording.
9. **`web/lib/regime*.ts` and similar shared helpers** — these may have references from kept components. PR-B needs a grep pass before deletion.

## Open questions (Q-prep — answer before PR-A)

Most v1 open questions are now resolved. Remaining ones:

- **Q1 (resolved):** `reports/performance_explainer_report.py` is **portfolio-bound** — reads only `data/performance.json` and `web/lib/chart-system-spec.json` to render an HTML explainer for the `/performance` page. Pure presentation, no scanner deps. KEEP.
- **Q2 (resolved):** No external callers of removed CLI entries in `~/projects/`. Empty grep.
- **Q3 (resolved):** `/api/options/chain` and `/api/options/expirations` are used by `OptionsChainTab.tsx` + `useChainPrefetch.ts` for order entry. Both call `xenon-ib-option-chain` (IB-only subprocess). KEEP.
- **Q4 (resolved):** Combo wizard is the active flow — `OrderTab.tsx`, `OptionsChainTab.tsx`, `useWizardSession.ts`, `useWizardLauncher.ts` all hit `/api/wizard/*`. KEEP.
- **Q5 (decided):** Drop the `vcg_cri_loop` field from `/health`. Don't replace it; the surviving fields (`ib_gateway`, `ib_pool`, `uw`, `futu`, `snapshotter`, `order_submissions`, `flex_divergence`) already cover the operational surface.
- **Q6 (open, your call):** Should `EXA_API_KEY` + `web/app/api/ticker/info/route.ts` stay? It's research enrichment on ticker pages, used in `/[ticker]` flow. Not signal generation per se. Default answer: keep, since `[ticker]` is in scope.
- **Q7 (resolved):** `clients/massive_client.py` has no callers (grep empty). Drop the file + `MASSIVE_API_KEY` env var in PR-C.
- **Q8 (resolved):** `data/` subdirectory disposition:
  - `data/menthorq_cache/` → DELETE (only used by removed menthorq client + fetchers + cta_sync)
  - `data/cri_scheduled/` → DELETE (CRI scanner state)
  - `data/evidence/` → DELETE (scanner audit dumps)
  - `data/locks/cta-sync.lock` → DELETE (other lock files in `locks/` may exist for kept services — only this file is CTA-bound; keep the directory)
  - `data/service_health/` → DELETE (only consumer was `cta_sync_health.py`)
  - `data/price_history_cache/` → **KEEP** — `xenon/utils/price_cache.py` uses it and is called by `portfolio_performance.py`
  - `data/universe/` → KEEP for safety. Note: `xenon.execution.universe` (the Python module in `src/xenon/execution/universe.py`) is **distinct** from the data directory and stays — order-path `contract_normalize.py` depends on it.
- **Q9 (resolved):** Nothing in KEEP imports from `xenon.scanners._shared` or `xenon.scanners.scanner` (grep empty). `src/xenon/scanners/` is a clean delete. The order-path `xenon.execution.universe` is the unrelated module.

## Estimated impact (refined)

- **Python LOC removed:** ~22,000 (out of ~78,000) — same as v1
- **Web LOC removed:** ~10,000 (out of ~28,000) — same as v1
- **`server.py` LOC removed:** ~600–800 of ~4,000 (15 inline routes + helpers + lifespan)
- **CLI entries removed:** ~30 (out of ~52) — same as v1
- **DB tables left orphaned:** ~15 (deferred to PR-E)
- **PRs:** 4 initial + 1 follow-up migration PR
- **Test suite shrinks:** roughly ~25% of `scripts/tests/` becomes obsolete

## Confidence (post-adversarial-review)

| Claim                                                                | Confidence | Evidence                                                                  |
| -------------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------- |
| KEEP/REMOVE buckets for `src/xenon/` subdirs                         | 95%        | Verified via top-level imports + dep graph                                |
| `monitor_daemon/` is position-rules enforcement, not signal          | 95%        | Handler filenames + dep grep — no signal imports                          |
| `combo_wizard/` is the active order entry flow                       | 95%        | OrderTab + OptionsChainTab + useWizardSession all hit it                  |
| `/options/chain`, `/options/expirations` are IB-only                 | 95%        | Both subprocess to `xenon-ib-option-chain`                                |
| `server.py` surgery list (15 inline routes + helpers)                | 90%        | Grep'd all `@app.<method>` decorators; mapped each to caller              |
| Portfolio reports' UW dep is real and blocking                       | 95%        | Direct line refs in `portfolio_performance.py`/`portfolio_report.py`      |
| No external `~/projects/` callers of removed CLIs                    | 90%        | Grep was empty; can't rule out hidden cron                                |
| ChatPanel has no tool calls                                          | 95%        | Read the route handler end-to-end                                         |
| Postgres tables can be left orphaned                                 | 85%        | No write callers in kept code, but reads may exist in unmigrated services |
| `useUwStatsHistory.ts` cleanup is complete                           | 80%        | Only Sidebar uses it per grep, but didn't read every consumer             |
| PR-A is mechanical (no architectural surprises beyond the server.py) | 80%        | All deps mapped; small risk of test fixtures referencing removed modules  |
| Identity/CLAUDE.md rewrite (PR-D) won't break anything functional    | 75%        | Subjective; docs-only PR, low blast radius                                |
| EXA_API_KEY / MASSIVE_API_KEY can be safely removed without refactor | 70%        | Q-prep pending                                                            |
| Estimated LOC counts (~22k Python, ~10k web)                         | 80%        | Calculated from `wc -l` per directory; minor over/under expected          |
| Overall plan is correct and executable in the stated 4+1 PR sequence | **90%**    | Net of above                                                              |

## Non-goals

- Renaming the project. "Xenon" stays.
- Removing the Postgres audit trail or any of the order-path safety guards.
- Changing the IB Gateway connection model (live remote / paper local).
- Touching the position-rules engine — that's the active feature on `feature/position-rules-implementation`.
