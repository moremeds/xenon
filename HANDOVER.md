# Handover — Performance tab rebuild

You are picking up brainstorming work in a worktree. The previous session designed a spec for rebuilding the broken `/performance` tab and persisting Futu NAV. The spec went through 3 versions and 2 full tribunal review rounds (Codex + Adversarial). It is committed but the **third verification pass was skipped at the user's request** and the **implementation has not started**.

## Where to start

1. Read the spec in full: `docs/superpowers/specs/2026-05-31-performance-rebuild-design.md` (560 lines). It is the source of truth. Do not re-litigate decisions there unless something is provably wrong.
2. Read this handover.
3. Decide with the user: do one more tribunal pass on v3, or go straight to writing the implementation plan via the `writing-plans` skill.

## What's broken in production today

Verified live on 2026-05-31:

- `/api/performance` returns a flat-line equity curve at $65,198.32 for every one of 102 trading days (`flat_days=100`, `positive_days=0`, `negative_days=1`). Sharpe −1.58, Max DD −0.02% are degenerate noise from a flat series.
- Clicking the Futu tab does NOT change the performance panel. Both tabs show the IB number. The panel ignores `activeAccount`.
- Futu's `POST /futu/sync` writes `data/futu_portfolio.json` but **does not** write to `xenon.nav_history` — there is no FUTU performance history to compute from, even if the panel wiring were fixed.

Screenshots of the broken state are in `.playwright-mcp/` (gitignored): `performance-initial.png`, `performance-current.png`, `performance-after-futu-click.png`.

## What the spec proposes (one paragraph)

Replace the 180-second `xenon-portfolio-perf` subprocess with a ~100ms inline FastAPI service that reads `xenon.nav_history` directly. Add a `?broker=IB|FUTU` query param + a new `get_performance_scope` FastAPI dependency. Persist FUTU NAV from the FastAPI `/futu/sync` hot path (not the CLI). Cache the resolved Futu account on `app.state.futu_account` (warmed at boot from the latest FUTU nav row so restarts don't break the panel). Ship v1 as "NAV change" (not TWR) and **mask all FUTU risk metrics** until cash-flow tracking lands as a follow-up. New `xenon.benchmark_closes` cache table for SPY closes. New `nav_history.source` column ('close' | 'intraday') so v2 can add an EOD scheduler without re-keying old rows. Partial unique index `nav_history_one_env_per_day` to make dual-curve account_env collisions impossible at the DB level. Discriminated-union `PerformanceData` type so the existing panel can branch on `status` without crashing.

## Tribunal trail

| Version      | Outcome                                              | Issues raised | Resolved                                       | New              |
| ------------ | ---------------------------------------------------- | ------------- | ---------------------------------------------- | ---------------- |
| v1 (2237a81) | NEEDS_REVISION                                       | 22            | —                                              | —                |
| v2 (ae3b40c) | NEEDS_REVISION                                       | —             | 17 of 22 cleanly; 3 partial; 2 minor           | 11 new patch-ups |
| v3 (0a111ea) | **NOT VERIFIED** — user skipped the third codex pass | —             | All v2 patch-ups addressed (claim, unverified) | Unknown          |

The v1 and v2 codex outputs + adversarial transcripts are in this session's transcript; key findings are summarized in the spec's `## Revision history` section.

## Decisions you should NOT relitigate

These are the load-bearing choices the user already made. Don't pull on these threads unless you have a concrete reason:

1. **Source of truth = `xenon.nav_history`**, not trade-replay. (Decisions §1)
2. **Inline FastAPI service, no subprocess.** (Decisions §2)
3. **SPY is the benchmark for both IB and Futu** — operator's call against my recommendation that we hide benchmark for Futu. Beta/Alpha may read low for non-US holdings; that's accepted. (Decisions §3)
4. **"NAV change" framing for v1, not TWR.** IB uses `daily_pnl/prev_nav` (IB's `dailyPnL` excludes cash flows). FUTU has no equivalent and is therefore **masked from risk metrics entirely** — same UX as the <30-session ladder — until a cash-flow tracking follow-up. (Decisions §8)
5. **Threshold ladder: <5 sessions = empty state; 5-30 = curve only; 30+ = full panel for IB.** (Decisions §4)
6. **Futu's `broker_account` lives on `app.state.futu_account`**, populated by lifespan from latest FUTU nav row AND refreshed on each successful `/futu/sync`. (Decisions §9)
7. **Existing visual shell is preserved** — the panel layout works; the data pipeline gets rebuilt under it.

## Decisions worth a second look

These are spec choices that were made with low conviction or where the reviewer pushed back hardest. If you spot a problem in implementation, these are the first places to question:

- **Decisions §8 (NAV vs TWR for IB)** — claim that IB's `reqPnL().dailyPnL` excludes deposits is **uncited** in the spec. Verify against IB TWS API docs before relying on it. If it includes deposits, IB returns are corrupted on deposit days too and the FUTU masking pattern should extend to IB.
- **Decisions §10 (FUTU env from `_matched_acc.trd_env`)** — the spec assumes `FutuClient._matched_acc` is a stable attribute. Verify the field exists at `src/xenon/clients/futu_client.py` and that it survives reconnects. The connect-time-fallback edge case is real (user-reported by the adversarial reviewer).
- **Decisions §13 (partial unique index)** — `CREATE UNIQUE INDEX nav_history_one_env_per_day` excludes `account_env` from the index columns. Confirm this is the correct Postgres syntax (it is, but verify it composes with the existing PK without conflict).
- **Threshold of 30 daily sessions for risk metrics** — adversarial reviewer pointed out 30 daily observations still has Sharpe SE ≈ √(252/30) ≈ 2.9, which is high. The user accepted 30 as a pragmatic UX floor, not a statistical one. Spec language was softened but the threshold itself is operator-tuned via `XENON_PERF_MIN_DAYS_METRICS` — fine to tune in implementation.
- **FUTU cash-flow contamination workaround** — masking everything but the equity curve is the v1 safety net. If the user pushes back ("but I want Sharpe for Futu!"), the right answer is to ship cash-flow tracking, not to unmask the metric. See the follow-up backlog.

## What the implementation plan needs to cover

The `writing-plans` skill should expect to break this into the following work units. Each is independently testable:

1. **Schema migrations** (3 Alembic revisions): `benchmark_closes` table, `nav_history.source` column with `'intraday'` default + check, partial unique index `nav_history_one_env_per_day`. Plus `schema.py` Table/Column object updates.
2. **`account_scope.py`** — `resolve_from_env()` rejects FUTU (CLI/env path is IB-only); add an `env_from_trd_env()` helper.
3. **`futu_client.py`** — expose `_matched_acc` or add a `trd_env_of_matched_account()` accessor. Verify the field exists.
4. **`futu_nav_persistence.py`** (new shared helper) — guards: `_acc_id is None` early return, missing `net_liquidation` early return, cross-env conflict raises typed exception → 409.
5. **`server.py` POST /futu/sync** — add `request: Request` param, call `persist_futu_nav`, mutate `app.state.futu_account/futu_trd_env`.
6. **`server.py` lifespan** — warm `app.state.futu_account` from latest FUTU `nav_history` row.
7. **`performance_metrics.py`** (new, extracted from `portfolio_performance.py`) — pure numpy math, no I/O. Mask logic for FUTU + <30-session lives in the service, NOT in metrics.
8. **`performance_service.py`** (new) — async; uses `get_engine()`; orchestrates `load_nav_curve` + `load_benchmark_cached` + `performance_metrics`. Branches on broker for masking and on row count for the threshold ladder.
9. **`nav_history.py`** (new query module) — `load_nav_curve`, `load_benchmark_cached` returns `(df, error_reason)`, `fetch_and_cache_benchmark` via IB pool data role.
10. **`routes/performance.py`** (new router) — `GET /performance?broker=...`, `get_performance_scope` dep. Deprecated POST stubs default broker=IB; `POST /performance/background` returns 202 immediately.
11. **`ib_sync._append_nav_snapshot`** — add the same cross-env guard for symmetry (`raise NavAccountEnvConflict`).
12. **`portfolio_performance.py` + `performance_explainer_report.py`** — `DeprecationWarning` at import.
13. **Web type contract** — `web/lib/types.ts` discriminated union + nullable `PerformanceSummary` fields.
14. **`PerformancePanel.tsx`** — branch on `data.status` BEFORE destructuring. Render the 4 empty states. Hero shows currency. `fmtPct`/`fmtRatio` accept null → `---`. Render warnings.
15. **`performanceChart.ts`** — gate on `data.status === "ok"` before reading `summary.starting_equity`. Skip null benchmark points.
16. **`usePerformance.ts`** — `(active, activeAccount)` signature; broker in cache key; `extractTimestamp` union-aware.
17. **`WorkspaceSections.tsx`** — forward `activeAccount` to `PerformancePanel`.
18. **`/api/performance/route.ts`** — broker query param proxy, drop in-process cache.
19. **Tests** — see the spec's `## Testing` section. ~12 new test files; 2 old test files DELETED (`scripts/tests/test_portfolio_performance.py`, `scripts/tests/test_performance_lock.py`).
20. **E2E** — `performance-broker-switch.spec.ts` asserts IB and FUTU hero values differ (not equal). `performance-futu-cold-start.spec.ts` covers the no-prior-sync case.

## Files NOT in the affected-files table but adjacent

Watch for these — they may need touch-up during implementation:

- `web/lib/performanceFreshness.ts` — `isPerformanceBehindPortfolioSync(data, ...)` — does it handle the union? Check before merge.
- `scripts/checks/no_json_fallback_on_order_path.py` allowlist — performance is not an order-path surface so this should not need touching, but verify.
- `docs/reference/order-path-incident-history.md` — append a row if anything in this work surfaces an incident pattern.
- Any test snapshot file (Vitest stores them under `__snapshots__/`) that captured the old `PerformanceData` shape.

## How to verify in-browser when implementing

Per `web/CLAUDE.md`, all UI changes need browser verification. The recommended flow:

1. `scripts/infra/dev.sh paper` (or `live`) — bring up IB Gateway + dev server
2. `http://localhost:3000/performance` — load the page
3. Click between IB and Futu tabs — assert the hero number and YTD percent change between tabs
4. For FUTU cold-start, restart FastAPI without triggering `/futu/sync` first — assert `insufficient_history reason=futu_not_synced` renders

If `chrome-cdp` skill is available, prefer it over Playwright. Both work.

## Housekeeping notes left for you

- The 3 spec commits live on this branch (`docs/performance-rebuild-spec`) in this worktree. Master is back at `origin/master`.
- Three browser-verification screenshots from the brainstorming session were moved into `.playwright-mcp/` (gitignored) in the main worktree. They're not in this branch's working tree.
- The codex v3 verification was started but skipped at the user's request. If you want it, the prompt is at `/tmp/codex-design-review-v3-out.txt` (may have been cleared between sessions — regenerate if needed).
- This handover file (`HANDOVER.md`) is NOT committed. Decide whether to commit it (under `docs/handovers/` per the existing convention) or delete it before opening a PR. Convention is to commit handovers — see `docs/handovers/` for prior examples.

## Suggested first message to send

> I'm picking up the performance tab rebuild from a prior session. The spec is at `docs/superpowers/specs/2026-05-31-performance-rebuild-design.md` and I've read the handover at `HANDOVER.md`. Before I invoke writing-plans, do you want one more codex verification pass on v3 (skipped last time), or should we go straight to plan-writing?

That gets you re-grounded with the user without redoing decisions.
