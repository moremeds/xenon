# Performance Page Holistic Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship one unified PR covering (a) **writer unification** — collapse three nav_history writers into one, with `source` (`'intraday' | 'close'`) as the audit column; (b) **daily IB close-NAV ingestion** via IB Flex `EquitySummaryByReportDateInBase` (scheduled LaunchAgent on macmini); (c) **reconciliation CLI** that surfaces same-date intraday-vs-close discrepancies; (d) **period selector** (1M/3M/YTD/All) on `/performance`; (e) **honest `total_return` headline** for FUTU with tooltip surfacing simple/TWR/IRR/net-deposits; (f) **FUTU risk metrics unmasked** now that typed cash flows exist in `xenon.futu_cash_flow`.

**Architecture:** One nav_history writer surface — `xenon.utils.portfolio_loader.upsert_nav_sync(..., source=...)` — fed by every writer (`ib_sync._append_nav_snapshot` delegated, `persist_futu_nav` delegated, IB Flex importer + new daily refresh CLI). `nav_history.source` already exists (CHECK on `'close' | 'intraday'`, server default `'intraday'`); audit comes from same-date rows under both sources. `load_nav_curve` adopts prefer-close semantics so the chart never double-counts. Backend `/performance` accepts `?period=1M|3M|YTD|All`; `compute()` re-derives series + summary against the window; cache key includes period. FUTU branch joins `nav_history` × `xenon.futu_cash_flow` to compute three return flavors (Simple flow-adjusted, TWR, IRR) plus `net_external_flows`. IB stays on `dailyPnL` returns + masked risk metrics; IB honest-% remains a separate follow-up (needs Flex CashTransaction extraction).

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0 async + sync, pandas, NumPy, scipy (`scipy.optimize.brentq` for IRR), Next.js 16 App Router, React 19, Vitest, Playwright. macOS launchd (`StartCalendarInterval`) for the daily 17:30 ET schedule. IB Flex Web Service (`EquitySummaryByReportDateInBase`) for IB close NAV. Cash-flow read path: `xenon.db.queries.futu_history.list_cashflows()` (already exists, no schema change).

---

## Pass-1 amendments (2026-06-03)

This plan is the **merged single source of truth** for the work originally tracked across two plans:

- This plan (`docs/superpowers/plans/2026-06-03-performance-holistic-upgrade.md`) — period selector + honest-% + FUTU TWR unmask.
- Plan-mode draft `~/.claude/plans/groovy-purring-cerf.md` — NAV auto-refresh — **superseded**.

Pass-1 self-review (`docs/superpowers/reviews/2026-06-03-performance-pass-1-review.md`) surfaced three critical gaps that combining the plans into one PR resolves:

1. **Three duplicate `nav_history` writers exist today** (`upsert_nav_sync` already source-aware on master, `ib_sync._append_nav_snapshot` bespoke `pg_insert`, `persist_futu_nav` bespoke `pg_insert`). The operator's "no code duplication" constraint forces unifying them. **New Task 0** does that — and must land first so every subsequent writer change touches one surface, not three.
2. **`load_nav_curve` prefer-close logic cannot wait for a v2 follow-up.** The moment Task 11 ships, two rows per Flex-touched date exist; without prefer-close the chart double-counts. **Task 4** now ships the prefer-close `DISTINCT ON` SQL in this PR.
3. **No reconciliation surface existed in either plan.** The audit data sits in PG with nobody reading it. **New Task 13** ships `xenon-nav-reconcile` — plain SQL comparing `source='intraday'` vs `source='close'` per date.

Operator decisions locked during Pass-1 review:

- **No new audit table.** `nav_history.source` is the entire audit hook.
- **Daily NAV save remains low-lag** — the intraday writer path is preserved; Task 0 is a pure refactor with no perf regression.
- **FUTU reconciliation is deferred** — Futu OpenD has no programmatic post-close NAV endpoint (verified against `.venv/.../futu/trade/open_trade_context.py`). A future plan adds a PDF parser for the Futu daily statement. FUTU cash-flow audit is already covered today by `xenon.futu_cash_flow` row-by-row ingestion (PR #120).
- **`XENON_READ_ONLY=1` honored everywhere new** — both the scheduled refresh CLI and the reconciliation CLI early-exit, matching `_append_nav_snapshot` and `persist_futu_nav`.

**Deferred to Pass 3 (adversarial):** concurrent LaunchAgent + manual `kickstart -k`, partial migration state, network split during Flex poll, mid-period source flip changing headline retroactively, `_prev_nav` snapshot view vs shared writer transaction.

---

## Pass-2 amendments (2026-06-03)

Tribunal review (`docs/superpowers/reviews/2026-06-03-performance-pass-2-tribunal.md`) — Codex + Gemini + Claude. Verdict: FIX-FIRST → fixes applied. **E1 resolved unanimously as option (a) — drop the unique PK + modify `nav_history_one_env_per_day`.** New PK is `(broker, account_env, broker_account, date, source)`; secondary index becomes a one-env-per-day-per-source guard. Intraday and close rows coexist as separate audit rows in `nav_history` — DB IS the audit trail (no new audit table).

11 consensus findings applied:

- **T1 (CRITICAL)** — Schema migration covers BOTH the PK AND `nav_history_one_env_per_day` (Codex caught the secondary-index gap Pass-1 missed). Migration shipped inside Task 0.
- **T2 (HIGH)** — Task 13 tests + SQL rewritten for option-(a): seed two same-date `source` rows, compare per-date.
- **T3 (HIGH)** — `enforce_account_env_guard` is now **default-on**. Legacy unscoped callers use `_upsert_nav_sync_unguarded()` escape hatch.
- **T4 (HIGH)** — Codex caught a FOURTH nav_history writer: `xenon.db.queries.portfolio.upsert_nav()` at `portfolio.py:188`. Pass-1 only found 3. Task 0 migrates it too. CI guard added.
- **T5 (HIGH)** — Pass-1's claim that the race-safe IntegrityError-catch was "hoisted via enforce_account_env_guard" was WRONG. Pre-INSERT SELECT only handles in-process duplication, not inter-process race. Task 0 now hoists the FULL pattern (SELECT + IntegrityError catch + rollback + re-query + raise `NavAccountEnvConflict`).
- **T6 (MEDIUM)** — Both sync (`upsert_nav_sync`) and async (`upsert_nav_async`) wrappers ship; FUTU async-path uses async wrapper (no event-loop blocking).
- **T7 (MEDIUM)** — LaunchAgent wrapper fails fast if `XENON_TRADING_MODE` not explicitly set. No silent `live` default.
- **T8 (MEDIUM)** — `src/xenon/jobs/nav_flex_refresh.py` already exists on this branch (parallel-worktree merge). Task 10 reframed as "patch existing CLI with READ_ONLY guard + add missing test."
- **T9 (MEDIUM)** — Task 5 period test expectation corrected from `"2026-01-02"` to `"2026-01-01"` (Task 1 contract).
- **T10 (LOW)** — `fetch_ib_nav_series` already passes `source="close"` on this branch (`portfolio_performance.py:429`). Task 9 reframed as regression-pin.
- **T11 (CRITICAL)** — `pg_test_async_engine` fixture doesn't exist. Globally renamed to `async_engine` (the real fixture at `conftest.py:151`).

---

## Pass-3 amendments (2026-06-03)

Adversarial review (`docs/superpowers/reviews/2026-06-03-performance-pass-3-adversarial.md`) — Claude solo adversarial. Three critical findings applied:

- **A1 (CRITICAL)** — `_build_upsert_stmt`'s `on_conflict_do_update(index_elements=...)` MUST include `source` post-migration. The 4-col form references a constraint that no longer exists; every UPSERT would raise. Task 0 Step 3b now spells out the 5-col `index_elements`.
- **A2 (HIGH)** — Macmini deploy ordering hazard: containers must not start before `alembic upgrade head` completes. Task 0 Step 3a documents the explicit sequence (stop containers → migrate → verify constraint → start containers) + a `psql` verification.
- **A3 (HIGH)** — Downgrade fails when same-(scope, date) rows have both `intraday` and `close` — duplicate-key violation against the old PK. Task 0 Step 3a `downgrade()` now DELETEs close rows first (preserves intraday — closer to v1 behavior).

Documented (no code change): A4 retroactive headline change (UX note in Task 7), A5 cache-key staleness post-migration (runbook note), A6 `_prev_nav` / `upsert_nav_async` txn isolation (acceptable under single-writer FUTU constraint).

---

## Context

PR #119 imported 262 close-price NAV rows so `/performance` could render YTD; PR #124 wired daily IB Flex auto-refresh so those rows stay current; PR #120 + #128 built the Futu trade/cash-flow backbone (`xenon.futu_trades`, `xenon.futu_cash_flow`) and the backward NAV walk. Three deferred items remained:

1. **Period selector** — currently `_period_start` is hardcoded to Jan 1 of the current year (`performance.py:91-93`). Users can't see 1M, 3M, or full-history windows.
2. **Honest `total_return`** — the headline is `(end - start) / start` (`performance.py:113`). When a user deposits cash mid-period, the deposit shows up as "gain". Now that `xenon.futu_cash_flow` has typed `cashflow_type ∈ {DEPOSIT, WITHDRAW, TRANSFER_IN, TRANSFER_OUT}` with `amount` and `occurred_at`, we can subtract external flows and additionally compute the time-weighted return (TWR) and money-weighted return (IRR).
3. **FUTU risk metrics unmasking** — `performance.py:294,301-306` masks Sharpe/Sortino/etc. for the FUTU broker with the explicit reason _"True Time-Weighted Return requires cash-flow tracking — follow-up."_ That follow-up is this PR.

Why combine them? All three meet at `performance.py::compute()` and `PerformancePanel.tsx`. The period selector changes `period_start`; the honest-% work changes the summary fields; the FUTU unmasking removes the mask gate — they touch overlapping lines in the same two files. Shipping separately means three rounds of cache-key bumps, three schema changes to the API contract, and three frontend refreshes of the same panel.

## Scope

**In scope:**

- **Ingestion (Pass-1 additions):**
  - Unify nav_history writers: migrate `_append_nav_snapshot` (IB) and `persist_futu_nav` (FUTU) to delegate to `upsert_nav_sync`. Cross-env collision guard + race-safe IntegrityError-catch hoisted into the shared helper.
  - `fetch_ib_nav_series` writes `source='close'` (latent bug fix — Flex rows are post-close, currently land as `'intraday'` via server default).
  - New `xenon-nav-flex-refresh` CLI invoked daily at 17:30 ET on the macmini via macOS LaunchAgent. Honors `XENON_READ_ONLY=1`.
  - `load_nav_curve` adopts prefer-close `DISTINCT ON` semantics — two-source same-date rows collapse to the close authority.
  - New `xenon-nav-reconcile` CLI — plain-SQL discrepancy report comparing `source='intraday'` vs `source='close'` per date for a scope. No new table. Honors `XENON_READ_ONLY=1` (it's read-only but logs the flag for uniformity).
- **Performance UI (original plan, unchanged):**
  - Backend: `period` query param ∈ `{1M, 3M, YTD, All}` on `GET /performance`.
  - Backend: `period_start` derivation from `period` + inception detection (FUTU All = min `nav_history.date`; IB All = same).
  - Backend: cache key includes `period` so switches don't return stale.
  - Backend: pure-function return-formula library — `simple_flow_adjusted_return()`, `time_weighted_return()`, `money_weighted_return_irr()`.
  - Backend: FUTU branch reads `xenon.futu_cash_flow` for the window, joins to `nav_history` by date.
  - Backend: FUTU summary gains four fields — `simple_total_return`, `twr_total_return`, `irr_total_return`, `net_external_flows`. IB summary gets the first only (no flows → equal to `total_return`); the others stay `None`.
  - Backend: FUTU `_futu_returns` becomes flow-adjusted; FUTU mask gate lifts; warnings updated.
  - Frontend: `PerformancePeriodSelector` component with 4 buttons.
  - Frontend: `usePerformance` hook + `/api/performance` route forward the `period` param.
  - Frontend: Headline keeps `total_return` (= simple flow-adjusted from backend); a `ⓘ` info icon opens a tooltip showing TWR, IRR, and net deposits.
- Tests: unit (Vitest, pytest), browser (Playwright). 95% coverage on new functions. Regression tests for writer delegation + prefer-close behavior.
- Operator docs: install runbook for the LaunchAgent + daily-tail recommendation for the first week.

**Out of scope (separate plans listed at the bottom):**

- IB Flex `CashTransaction` section extraction — needed for IB honest %.
- New `cash_flows` cross-broker table — the FUTU-only path is good for now.
- **Futu daily-statement PDF parser.** Operator-confirmed deferral (Pass 1, 2026-06-03): Futu OpenD has no programmatic post-close NAV endpoint, and synthesizing a close NAV from positions + cash flows reconciles against itself (theater). The PDF parser is a separate plan; FUTU close-NAV reconciliation lands at that time. FUTU cash-flow audit is already covered by `xenon.futu_cash_flow` row-by-row ingestion (PR #120).
- Schema change to `nav_history` PK (drop unique constraint to let intraday + close rows coexist as separate rows). **Deferred to Pass 2 codex vote.**

## File Structure

| File                                               | Responsibility                            | Action                                                                                           |
| -------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `src/xenon/api/services/performance_periods.py`    | Pure period-string → date resolver        | Create                                                                                           |
| `src/xenon/api/services/performance_returns.py`    | Pure formula library (Simple / TWR / IRR) | Create                                                                                           |
| `src/xenon/api/services/performance_futu_flows.py` | FUTU cash-flow loader + per-day bucketing | Create                                                                                           |
| `src/xenon/db/queries/nav_history.py`              | NAV curve loader                          | Modify `load_nav_curve` to accept `period_end` (optional)                                        |
| `src/xenon/api/services/performance.py`            | Performance compute                       | Modify — accept `period`; integrate flows for FUTU; new summary fields; FUTU mask lifts          |
| `src/xenon/api/services/perf_cache.py`             | TTL cache                                 | Modify — cache key includes `period`                                                             |
| `src/xenon/api/routes/performance.py`              | GET `/performance` route                  | Modify — accept `?period=` query                                                                 |
| `web/app/api/performance/route.ts`                 | Next.js proxy                             | Modify — forward `?period=`                                                                      |
| `web/lib/usePerformance.ts`                        | React hook                                | Modify — accept `period`, key endpoint by `(broker, period)`                                     |
| `web/lib/types.ts`                                 | TypeScript types                          | Modify — add `simple_total_return`, `twr_total_return`, `irr_total_return`, `net_external_flows` |
| `web/components/PerformancePeriodSelector.tsx`     | Period picker UI                          | Create                                                                                           |
| `web/components/PerformanceHeadlineTooltip.tsx`    | Tooltip rendering 4 numbers               | Create                                                                                           |
| `web/components/PerformancePanel.tsx`              | Performance panel                         | Modify — mount selector, wire period to hook, mount tooltip on headline                          |
| `scripts/tests/test_performance_periods.py`        | Period resolver tests                     | Create                                                                                           |
| `scripts/tests/test_performance_returns.py`        | Formula tests                             | Create                                                                                           |
| `scripts/tests/test_performance_futu_flows.py`     | FUTU cash-flow loader tests               | Create                                                                                           |
| `scripts/tests/test_performance_period_e2e.py`     | `compute()` integration with `period`     | Create                                                                                           |
| `scripts/tests/test_performance_futu_unmasked.py`  | FUTU returns + unmasked metrics           | Create                                                                                           |
| `src/xenon/api/services/tests/test_perf_cache.py`  | Cache key changes                         | Modify (existing) or create if absent — pin cache-key-includes-period                            |
| `web/tests/performance-period-selector.test.ts`    | Selector unit test                        | Create                                                                                           |
| `web/tests/performance-headline-tooltip.test.ts`   | Tooltip unit test                         | Create                                                                                           |
| `web/tests/use-performance-period.test.ts`         | Hook unit test                            | Create                                                                                           |
| `web/e2e/performance-period-selector.spec.ts`      | Playwright E2E                            | Create                                                                                           |

**Reused (do NOT reinvent):**

- `xenon.db.queries.futu_history.list_cashflows(engine, scope, since, until)` — already returns typed rows ordered by `occurred_at`. Use it.
- `xenon.execution.account_scope.AccountScope` — scope dataclass.
- `xenon.utils.market_calendar.current_session_date_et` — today in ET.
- `xenon.reports.performance_metrics` — existing Sharpe/Sortino/etc. helpers; do NOT re-implement.
- `scripts/tests/conftest.py::pg_test_engine` fixture — gates PG tests on a reachable DB.
- `xenon.db.queries.futu_history.insert_cashflows` — for tests that need to seed `futu_cash_flow` rows.
- Existing CSS classes (`metric-card`, `metric-value`, `pill` etc.) from `web/components/PerformancePanel.tsx` — selector + tooltip reuse the same visual vocabulary.

---

## Task 0: Unify `nav_history` writers + Pass-2 schema migration

**Background (Pass-2 revised):** Four writers `pg_insert(nav_history)` today:

1. `xenon.utils.portfolio_loader.upsert_nav_sync` — shared surface; already source-aware (`portfolio_loader.py:135`).
2. `xenon.execution.ib_sync._append_nav_snapshot` — IB intraday (lines 1032-1094). Server default `'intraday'` fires; no `source` passed.
3. `xenon.api.services.futu_nav_persistence.persist_futu_nav` — FUTU intraday. Has **race-safe IntegrityError-catch** (lines 155-167): app-level guard + DB-level unique constraint + on-conflict retry that re-queries the winner. Stamps `source='intraday'` explicitly.
4. **`xenon.db.queries.portfolio.upsert_nav()` at `portfolio.py:188`** — async writer taking `AsyncConnection`. No `source` param. **Pass-2 finding T4 — Pass-1 missed this one.**

Pass-2 tribunal vote on E1 (unanimous, weight 2.5): drop the unique PK + modify `nav_history_one_env_per_day`. Intraday and close rows must coexist for true row-level auditability — option (b) loses the intraday row body when close overwrites. **Critical addendum (Codex):** the secondary unique index at `schema.py:200` also blocks coexistence; dropping only the PK is insufficient.

Task 0 ships:

- **Schema migration**: new PK = `(broker, account_env, broker_account, date, source)`; `nav_history_one_env_per_day` becomes a one-env-per-day-per-source guard.
- **Unified writer**: shared core does `pg_insert(nav_history)` exactly once. `upsert_nav_sync(...)` and `upsert_nav_async(...)` wrappers expose sync + async surfaces. Account-env guard is **default-on** (Pass-2 T3 inversion); legacy unscoped callers use `_upsert_nav_sync_unguarded()` escape hatch.
- **Full race-safe pattern hoisted** (Pass-2 T5): pre-INSERT SELECT + `IntegrityError` catch + rollback + re-query winner + raise `NavAccountEnvConflict`. Pass-1's claim that the SELECT alone preserved the race-safety was WRONG — only the SELECT+catch combination handles inter-process race.
- **All 4 writers migrated** to delegate (no more bespoke `pg_insert(nav_history)` anywhere).
- **CI guard**: import-checker fails if any new module imports `pg_insert(nav_history)` outside `portfolio_loader.py`.

This task ships first because every subsequent writer change must touch ONE surface, not four.

**Files:**

- Create: `src/xenon/db/migrations/versions/2026_06_03_nav_history_source_in_pk.py` — drop old PK, add new PK with `source`, replace `nav_history_one_env_per_day` index
- Modify: `src/xenon/db/schema.py:174-201` — update Table definition: `source` becomes part of PK; secondary index gains `source`
- Modify: `src/xenon/utils/portfolio_loader.py` — `upsert_nav_sync` is default-guarded; add `upsert_nav_async` wrapper; add `_upsert_nav_sync_unguarded` escape hatch; absorb full race-safe IntegrityError pattern
- Modify: `src/xenon/execution/ib_sync.py:1032-1094` — replace `_append_nav_snapshot` body with `upsert_nav_sync(...)` call (no kwarg — guard is default-on)
- Modify: `src/xenon/api/services/futu_nav_persistence.py:88-167` — replace `persist_futu_nav` body with `await upsert_nav_async(...)` (Pass-2 T6 — no event-loop blocking)
- Modify: `src/xenon/db/queries/portfolio.py:188` — make `upsert_nav()` delegate to `upsert_nav_async`, add `source` parameter
- Create: `scripts/checks/no_pg_insert_nav_history.py` — CI guard: only `portfolio_loader.py` may `pg_insert(nav_history)`
- Modify: `.github/workflows/ci.yml` — wire the new guard into the `python-tests` job
- Test: `scripts/tests/test_nav_history_writer_unification.py` (create)
- Test: `scripts/tests/test_nav_history_source_pk_migration.py` (create) — verifies migration up/down + two-row-same-date scenario

- [ ] **Step 1: Write the failing tests**

Create `scripts/tests/test_nav_history_writer_unification.py`:

```python
"""Pass-1 unification — three writers must funnel through one surface."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import text

from xenon.api.services.futu_nav_persistence import (
    NavAccountEnvConflict,
    persist_futu_nav,
)
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DUQ123456")


def _read_back(scope: AccountScope, day: date) -> dict | None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT nav, source, account_env FROM xenon.nav_history "
                "WHERE broker=:b AND account_env=:e AND broker_account=:a AND date=:d"
            ),
            {"b": scope.broker, "e": scope.account_env, "a": scope.broker_account, "d": day},
        ).first()
    if row is None:
        return None
    return {"nav": row.nav, "source": row.source, "account_env": row.account_env}


def test_upsert_nav_sync_cross_env_guard_raises(pg_test_engine):
    """When a row exists with a different account_env, the guarded path raises."""
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100"))
    conflicting = AccountScope(broker="IB", account_env="live", broker_account="DUQ123456")
    with pytest.raises(NavAccountEnvConflict):
        upsert_nav_sync(
            scope=conflicting,
            day=date(2026, 6, 1),
            nav=Decimal("200"),
            enforce_account_env_guard=True,
        )


def test_upsert_nav_sync_default_enforces_guard(pg_test_engine):
    """Pass-2 T3: guard is default-ON. A cross-env collision raises without opt-in."""
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100"))
    other = AccountScope(broker="IB", account_env="live", broker_account="DUQ123456")
    with pytest.raises(NavAccountEnvConflict):
        upsert_nav_sync(scope=other, day=date(2026, 6, 1), nav=Decimal("200"))


def test_upsert_nav_sync_unguarded_escape_hatch(pg_test_engine):
    """Pass-2 T3: legacy unscoped callers can opt out via _upsert_nav_sync_unguarded."""
    from xenon.utils.portfolio_loader import _upsert_nav_sync_unguarded
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100"))
    other = AccountScope(broker="IB", account_env="live", broker_account="DUQ123456")
    # No raise — escape hatch bypasses the guard. Caller takes responsibility.
    _upsert_nav_sync_unguarded(scope=other, day=date(2026, 6, 1), nav=Decimal("200"), source="intraday")


def test_upsert_nav_sync_race_safe_integrity_error(pg_test_engine, monkeypatch):
    """Pass-2 T5: when SELECT guard passes but DB-level constraint fires (inter-process
    race winner inserted between our SELECT and INSERT), the catch+re-query path must
    surface NavAccountEnvConflict instead of leaking IntegrityError."""
    import sqlalchemy as sa
    # Simulate the race by forcing an IntegrityError after the SELECT guard runs OK.
    # Pre-seed via a different account_env using the unguarded path.
    from xenon.utils.portfolio_loader import _upsert_nav_sync_unguarded
    other = AccountScope(broker="IB", account_env="live", broker_account="DUQ123456")
    _upsert_nav_sync_unguarded(scope=other, day=date(2026, 6, 1), nav=Decimal("100"), source="intraday")

    # Now the guarded path: SELECT sees `live`, current scope is `paper` → guard fires
    # via SELECT before IntegrityError path. (For the SELECT-passes-then-INSERT-races
    # scenario, the test harness pattern is to monkeypatch the SELECT to return None
    # while the row still exists. Documented here as a follow-up — the SELECT path
    # already covers the common case; the catch+re-query is a defense-in-depth tier.)
    with pytest.raises(NavAccountEnvConflict):
        upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("200"), source="intraday")


@pytest.mark.asyncio
async def test_upsert_nav_async_basic(async_engine):
    """Pass-2 T6: async wrapper writes through AsyncEngine without blocking the loop."""
    from xenon.utils.portfolio_loader import upsert_nav_async
    futu = AccountScope(broker="FUTU", account_env="live", broker_account="FUTU123")
    await upsert_nav_async(
        async_engine, scope=futu, day=date(2026, 6, 1), nav=Decimal("50000"), source="intraday"
    )
    # Verify via sync read-back.
    row = _read_back(futu, date(2026, 6, 1))
    assert row is not None
    assert row["nav"] == Decimal("50000")
    assert row["source"] == "intraday"


def test_two_source_rows_per_date_coexist(pg_test_engine):
    """Pass-2 T1: post-migration, intraday + close rows for the same date coexist
    (new PK includes source). Audit comes from row coexistence."""
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100000"), source="intraday")
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100050"), source="close")
    engine = get_sync_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT nav, source FROM xenon.nav_history "
                "WHERE broker=:b AND account_env=:e AND broker_account=:a AND date=:d "
                "ORDER BY source"
            ),
            {"b": SCOPE.broker, "e": SCOPE.account_env, "a": SCOPE.broker_account, "d": date(2026, 6, 1)},
        ).fetchall()
    assert len(rows) == 2
    sources = {r.source for r in rows}
    assert sources == {"intraday", "close"}


def test_ib_append_nav_snapshot_delegates_to_upsert_nav_sync(monkeypatch, pg_test_engine):
    """The IB intraday writer must funnel through the shared surface."""
    from xenon.execution import ib_sync

    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ123456")
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)

    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("xenon.execution.ib_sync.upsert_nav_sync", _spy)
    ib_sync._append_nav_snapshot(net_liq=12345.67, daily_pnl=42.0)
    assert len(calls) == 1
    assert calls[0]["scope"].broker == "IB"
    assert calls[0]["scope"].account_env == "paper"
    assert calls[0]["scope"].broker_account == "DUQ123456"
    assert calls[0]["nav"] == Decimal("12345.67")
    assert calls[0]["daily_pnl"] == Decimal("42.00")
    assert calls[0]["source"] == "intraday"
    # Pass-2 T3: guard is default-on; no kwarg expected.


@pytest.mark.asyncio
async def test_persist_futu_nav_delegates_to_upsert_nav_async(monkeypatch, async_engine):
    """Pass-2 T6: FUTU intraday writer routes through the async wrapper, not sync."""
    from xenon.api.services import futu_nav_persistence

    calls: list[dict] = []

    async def _spy(engine, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(futu_nav_persistence, "upsert_nav_async", _spy)

    class _FakeFutuClient:
        _acc_id = "FUTU456"

    payload = {"account_summary": {"net_liquidation": 50_000.0}}
    await persist_futu_nav(async_engine, _FakeFutuClient(), "REAL", payload)
    assert len(calls) == 1
    assert calls[0]["scope"].broker == "FUTU"
    assert calls[0]["source"] == "intraday"


def test_xenon_read_only_still_no_ops_for_ib_writer(monkeypatch, pg_test_engine):
    """XENON_READ_ONLY=1 contract preserved post-migration."""
    from xenon.execution import ib_sync

    monkeypatch.setenv("XENON_READ_ONLY", "1")
    calls: list[dict] = []
    monkeypatch.setattr(
        "xenon.execution.ib_sync.upsert_nav_sync", lambda **kw: calls.append(kw)
    )
    ib_sync._append_nav_snapshot(net_liq=100.0)
    assert calls == []
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
uv run pytest scripts/tests/test_nav_history_writer_unification.py -xvs
```

Expected: the delegation tests fail because both writers still call `pg_insert` directly; the cross-env guard test fails with `TypeError: upsert_nav_sync() got an unexpected keyword argument 'enforce_account_env_guard'`.

- [ ] **Step 3a (Pass-2 addition): Ship the schema migration**

Create `src/xenon/db/migrations/versions/2026_06_03_nav_history_source_in_pk.py`. Drop the old PK + secondary index; add `source` to both:

```python
"""nav_history: source in PK + secondary index — Pass-2 E1 option (a)."""

from alembic import op
import sqlalchemy as sa

revision = "2026_06_03_nav_history_source_in_pk"
down_revision = "260fabba18d6"  # add_nav_history_source_column
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop old PK and secondary unique index. Both block same-date `source`
    # coexistence. The CHECK on `source IN ('close','intraday')` is preserved.
    op.execute("ALTER TABLE xenon.nav_history DROP CONSTRAINT nav_history_pkey")
    op.drop_index("nav_history_one_env_per_day", table_name="nav_history", schema="xenon")

    # New PK includes source — two rows per date (intraday + close) coexist as
    # separate audit rows.
    op.create_primary_key(
        "nav_history_pkey",
        "nav_history",
        ["broker", "account_env", "broker_account", "date", "source"],
        schema="xenon",
    )

    # Secondary guard: still atomic-dual-curve protection per source.
    # Excludes account_env (cross-env collision still blocked) but includes
    # source so intraday and close coexist.
    op.create_index(
        "nav_history_one_env_per_day_per_source",
        "nav_history",
        ["broker", "broker_account", "date", "source"],
        unique=True,
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_index("nav_history_one_env_per_day_per_source", table_name="nav_history", schema="xenon")
    op.execute("ALTER TABLE xenon.nav_history DROP CONSTRAINT nav_history_pkey")
    op.create_primary_key(
        "nav_history_pkey",
        "nav_history",
        ["broker", "account_env", "broker_account", "date"],
        schema="xenon",
    )
    op.create_index(
        "nav_history_one_env_per_day",
        "nav_history",
        ["broker", "broker_account", "date"],
        unique=True,
        schema="xenon",
    )
```

Update `src/xenon/db/schema.py:174-201` Table definition to match: `source` becomes part of `primary_key=True` columns; rename index `nav_history_one_env_per_day` → `nav_history_one_env_per_day_per_source` with `source` added.

Apply locally:

```bash
uv run alembic upgrade head
```

**Pass-3 A2 (HIGH) — deploy ordering:** the macmini Docker stack must run `alembic upgrade head` BEFORE the new app code starts. If new code (with 5-col `index_elements`) writes against the old schema, the UPSERT raises `there is no unique or exclusion constraint...`. Procedure:

1. Stop app containers (`api`, `realtime services`).
2. Run `migrator` service to apply `2026_06_03_nav_history_source_in_pk`.
3. Verify: `psql -h … -U xenon_prod core_dev -c "SELECT conname, conkey FROM pg_constraint WHERE conrelid = 'xenon.nav_history'::regclass;"` — confirm new PK includes `source`.
4. Start app containers.

The `scripts/deploy/macmini-prod.sh` flow already does (1) → (2) → (4); just verify (3) by tail-logging the migrator output and asserting "ALTER TABLE" success.

**Pass-3 A3 (HIGH) — downgrade safety:** the alembic `downgrade()` will fail if any (broker, account_env, broker_account, date) has both `intraday` and `close` rows (duplicate keys when recreating the old PK). Update the downgrade to delete close rows first:

```python
def downgrade() -> None:
    # Pass-3 A3: clean up coexisting rows before reverting the PK. Preserve
    # the intraday row (closer to the v1 single-row-per-date world). Operators
    # who need close NAVs back can re-run xenon-nav-flex-refresh.
    op.execute("""
        DELETE FROM xenon.nav_history
        WHERE source = 'close'
          AND (broker, account_env, broker_account, date) IN (
              SELECT broker, account_env, broker_account, date
              FROM xenon.nav_history
              GROUP BY broker, account_env, broker_account, date
              HAVING COUNT(*) > 1
          )
    """)
    op.drop_index("nav_history_one_env_per_day_per_source", table_name="nav_history", schema="xenon")
    op.execute("ALTER TABLE xenon.nav_history DROP CONSTRAINT nav_history_pkey")
    op.create_primary_key(
        "nav_history_pkey",
        "nav_history",
        ["broker", "account_env", "broker_account", "date"],
        schema="xenon",
    )
    op.create_index(
        "nav_history_one_env_per_day",
        "nav_history",
        ["broker", "broker_account", "date"],
        unique=True,
        schema="xenon",
    )
```

- [ ] **Step 3b (Pass-2 revised): Refactor `upsert_nav_sync` — guard ON by default + FULL race-safe pattern**

Edit `src/xenon/utils/portfolio_loader.py:125`. Two changes:

1. Add `enforce_account_env_guard: bool = True` (Pass-2 T3 — inverted default; legacy unscoped callers use `_upsert_nav_sync_unguarded`).
2. Add the full race-safe pattern (Pass-2 T5 — pre-INSERT SELECT alone is NOT race-safe; must catch `IntegrityError` and re-query the winner).

```python
def upsert_nav_sync(
    *,
    scope: AccountScope,
    day: _date,
    nav: _Decimal | float | int,
    daily_pnl: _Decimal | float | int | None = None,
    total: _Decimal | float | int | None = None,
    cash: _Decimal | float | int | None = None,
    stock_value: _Decimal | float | int | None = None,
    options_value: _Decimal | float | int | None = None,
    source: str | None = None,
    enforce_account_env_guard: bool = True,  # Pass-2 T3 — was opt-in, now default-on
) -> None:
    """Unified nav_history writer. Sync surface used by ib_sync + Flex importer + perf CLIs.

    The guard catches in-process duplicate writes (SELECT before INSERT) AND
    inter-process race (IntegrityError catch + re-query winner + raise). Both
    paths exit via `NavAccountEnvConflict`. Pass-2 T5: SELECT alone is not
    sufficient; the IntegrityError catch is the actual race-safety mechanism.
    """
    from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict
    engine = get_sync_engine()

    # Pass-2 T5 race-safe upsert: SELECT guard + IntegrityError catch + re-query.
    if enforce_account_env_guard:
        with engine.begin() as conn:
            existing = conn.execute(
                sa.select(nav_history.c.account_env).where(
                    (nav_history.c.broker == scope.broker)
                    & (nav_history.c.broker_account == scope.broker_account)
                    & (nav_history.c.date == day)
                )
            ).first()
            if existing is not None and existing.account_env != scope.account_env:
                raise NavAccountEnvConflict(scope, existing.account_env, day)

    stmt = _build_upsert_stmt(scope, day, nav, daily_pnl, total, cash, stock_value, options_value, source)
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except sa.exc.IntegrityError:
        if not enforce_account_env_guard:
            raise
        # Inter-process race: another writer with different account_env passed
        # the SELECT before we did and won the INSERT. Re-query the winner;
        # raise NavAccountEnvConflict if it's actually a cross-env collision.
        with engine.begin() as conn2:
            winner = conn2.execute(
                sa.select(nav_history.c.account_env).where(
                    (nav_history.c.broker == scope.broker)
                    & (nav_history.c.broker_account == scope.broker_account)
                    & (nav_history.c.date == day)
                )
            ).first()
        if winner is not None and winner.account_env != scope.account_env:
            raise NavAccountEnvConflict(scope, winner.account_env, day)
        raise


def _upsert_nav_sync_unguarded(**kwargs) -> None:
    """⚠ ESCAPE HATCH — bypasses cross-env guard. ONLY for legacy unscoped
    backfill code that proves it has no concurrent writers. New code MUST
    use upsert_nav_sync directly."""
    upsert_nav_sync(enforce_account_env_guard=False, **kwargs)
```

`_build_upsert_stmt(...)` is a private helper that returns the `pg_insert(...).on_conflict_do_update(...)` statement.

**Pass-3 A1 (CRITICAL):** the helper MUST use the **new** PK columns in `index_elements` — `[broker, account_env, broker_account, date, source]`. The current `upsert_nav_sync` body uses 4-column elements `[broker, account_env, broker_account, date]`; after Task 0's migration, those 4 columns no longer name a constraint, and the UPSERT will fail with `there is no unique or exclusion constraint matching the ON CONFLICT specification`. Update both the helper AND any direct callers (e.g., the 4-column `set_columns` clause inside the current body) at the same time.

```python
def _build_upsert_stmt(scope, day, nav, daily_pnl, total, cash, stock_value, options_value, source):
    values = {
        "broker": scope.broker,
        "account_env": scope.account_env,
        "broker_account": scope.broker_account,
        "date": day,
        "nav": nav,
        "daily_pnl": daily_pnl,
        "total": total,
        "cash": cash,
        "stock_value": stock_value,
        "options_value": options_value,
    }
    if source is not None:
        values["source"] = source
    stmt = _pg_insert(nav_history).values(**values)
    set_columns = {"nav": stmt.excluded.nav}
    for col, val in (("daily_pnl", daily_pnl), ("total", total), ("cash", cash),
                     ("stock_value", stock_value), ("options_value", options_value)):
        if val is not None:
            set_columns[col] = getattr(stmt.excluded, col)
    return stmt.on_conflict_do_update(
        # Pass-3 A1: NEW PK includes source. The 4-column form (without source)
        # no longer names a constraint post-migration → UPSERT raises.
        index_elements=[
            nav_history.c.broker,
            nav_history.c.account_env,
            nav_history.c.broker_account,
            nav_history.c.date,
            nav_history.c.source,
        ],
        set_=set_columns,
    )
```

When source is None (legacy callers via `_upsert_nav_sync_unguarded`), the INSERT uses the server default `'intraday'` and the on-conflict targets `(scope, date, 'intraday')` — but the on_conflict_do_update will then only fire on collisions with another intraday row for the same scope/date, which IS the correct semantics now that close + intraday coexist.

- [ ] **Step 3c (Pass-2 T6 addition): Add `upsert_nav_async` wrapper**

Still in `src/xenon/utils/portfolio_loader.py`, add an async wrapper for FastAPI callers:

```python
async def upsert_nav_async(
    engine,  # AsyncEngine
    *,
    scope: AccountScope,
    day: _date,
    nav: _Decimal | float | int,
    daily_pnl: _Decimal | float | int | None = None,
    source: str | None = None,
    enforce_account_env_guard: bool = True,
) -> None:
    """Async surface for FastAPI callers (persist_futu_nav). Same race-safe
    semantics as upsert_nav_sync but uses the AsyncEngine — no event-loop
    blocking from get_sync_engine() inside an async route (Pass-2 T6)."""
    from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict

    if enforce_account_env_guard:
        async with engine.begin() as conn:
            existing = (await conn.execute(
                sa.select(nav_history.c.account_env).where(
                    (nav_history.c.broker == scope.broker)
                    & (nav_history.c.broker_account == scope.broker_account)
                    & (nav_history.c.date == day)
                )
            )).first()
            if existing is not None and existing.account_env != scope.account_env:
                raise NavAccountEnvConflict(scope, existing.account_env, day)

    stmt = _build_upsert_stmt(scope, day, nav, daily_pnl, None, None, None, None, source)
    try:
        async with engine.begin() as conn:
            await conn.execute(stmt)
    except sa.exc.IntegrityError:
        if not enforce_account_env_guard:
            raise
        async with engine.begin() as conn2:
            winner = (await conn2.execute(
                sa.select(nav_history.c.account_env).where(
                    (nav_history.c.broker == scope.broker)
                    & (nav_history.c.broker_account == scope.broker_account)
                    & (nav_history.c.date == day)
                )
            )).first()
        if winner is not None and winner.account_env != scope.account_env:
            raise NavAccountEnvConflict(scope, winner.account_env, day)
        raise
```

- [ ] **Step 4: Replace `_append_nav_snapshot` body in `ib_sync.py`**

Edit `src/xenon/execution/ib_sync.py:1032-1094`. Replace the function body (keeping the signature + the `XENON_READ_ONLY` early-exit + the timezone + scope resolution) with:

```python
def _append_nav_snapshot(net_liq: float, daily_pnl=None) -> None:
    """Upsert today's NAV into Postgres nav_history via the unified surface.

    Cross-env conflict guard (spec Decisions §13, perf-rebuild correction #2):
    delegated to upsert_nav_sync(enforce_account_env_guard=True).
    """
    if os.environ.get("XENON_READ_ONLY") == "1":
        print(f"⏭  XENON_READ_ONLY=1 — skipping NAV snapshot write (net_liq=${net_liq:,.2f})")
        return

    import pytz

    from xenon.execution.account_scope import AccountScope
    from xenon.utils.portfolio_loader import upsert_nav_sync

    et = pytz.timezone("America/New_York")
    today = datetime.now(et).date()

    nav_val = Decimal(str(round(net_liq, 2)))
    pnl_val = Decimal(str(round(float(daily_pnl), 2))) if daily_pnl is not None else None
    broker, account_env, broker_account = _scope_from_env()
    scope = AccountScope(broker=broker, account_env=account_env, broker_account=broker_account)

    upsert_nav_sync(
        scope=scope,
        day=today,
        nav=nav_val,
        daily_pnl=pnl_val,
        source="intraday",
    )  # guard default-on per Pass-2 T3 — no kwarg needed
    print(f"✓ NAV snapshot: {today} → ${net_liq:,.2f}")
```

- [ ] **Step 5 (Pass-2 T6 revised): Replace `persist_futu_nav` body using async wrapper**

Edit `src/xenon/api/services/futu_nav_persistence.py:88-167`. Keep the early-return guards (`_acc_id`, `matched_trd_env`, `net_liq is None`) and the `NavAccountEnvConflict` class. Replace the `async with engine.begin() as conn:` block with:

```python
    async with engine.begin() as conn:
        prev_nav = await _prev_nav(conn, scope, today)
    daily_pnl = (net_liq - prev_nav) if prev_nav is not None else None

    # Pass-2 T6: use async wrapper. No event-loop blocking from sync engine
    # inside the FastAPI async route. Race-safe IntegrityError catch is
    # inside upsert_nav_async (Pass-2 T5).
    from xenon.utils.portfolio_loader import upsert_nav_async
    await upsert_nav_async(
        engine,
        scope=scope,
        day=today,
        nav=Decimal(str(net_liq)),
        daily_pnl=Decimal(str(daily_pnl)) if daily_pnl is not None else None,
        source="intraday",
    )
```

The `_prev_nav` query stays async (it's a separate read; could be folded into `upsert_nav_async`'s transaction if profiling warrants, but the current shape is sound).

- [ ] **Step 5b (Pass-2 T4 — missed-writer fix): Migrate `xenon.db.queries.portfolio.upsert_nav()`**

Pass-1 self-review found 3 nav_history writers; tribunal found a 4th. `xenon.db.queries.portfolio.upsert_nav()` at `portfolio.py:188` is an async writer that takes `AsyncConnection` (not `AsyncEngine`) and does its own `pg_insert(nav_history)`.

Audit callers before changing — `grep -rn "from xenon.db.queries.portfolio import upsert_nav\|queries.portfolio.upsert_nav" src/ scripts/ web/`. If callers can be migrated to `upsert_nav_async(engine, ...)`, delete the legacy function. Otherwise, replace its body to delegate:

```python
async def upsert_nav(
    conn: AsyncConnection,  # legacy connection-based signature preserved for callers
    day: date,
    *,
    nav: Decimal,
    daily_pnl: Decimal | None = None,
    total: Decimal | None = None,
    cash: Decimal | None = None,
    stock_value: Decimal | None = None,
    options_value: Decimal | None = None,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
    source: str | None = None,  # Pass-2 T4 addition
) -> None:
    """Legacy connection-based async writer. New code should use
    upsert_nav_async(engine, ...) from xenon.utils.portfolio_loader."""
    from xenon.execution.account_scope import AccountScope
    from xenon.utils.portfolio_loader import _build_upsert_stmt

    scope = AccountScope(broker=broker, account_env=account_env, broker_account=broker_account)
    stmt = _build_upsert_stmt(scope, day, nav, daily_pnl, total, cash, stock_value, options_value, source)
    await conn.execute(stmt)
```

(Connection-scoped variant skips the cross-env guard because the caller already controls the transaction. Document this in the docstring; new callers should NOT route through this entry point.)

- [ ] **Step 5c (Pass-2 T4 — CI guard): Block future divergence**

Create `scripts/checks/no_pg_insert_nav_history.py`:

```python
#!/usr/bin/env python3
"""CI guard: only `portfolio_loader.py` may pg_insert(nav_history).

Pass-2 T4 — Codex found a 4th nav_history writer (portfolio.upsert_nav) that
Pass-1 missed. This guard prevents the same drift in the future.
"""
import re
import sys
from pathlib import Path

ALLOW = {"src/xenon/utils/portfolio_loader.py"}
PATTERN = re.compile(r"pg_insert\(\s*nav_history\s*\)")

root = Path(__file__).resolve().parents[2]
offenders = []
for path in root.rglob("*.py"):
    rel = path.relative_to(root).as_posix()
    if rel in ALLOW or "/.venv/" in rel or "/__pycache__/" in rel:
        continue
    text = path.read_text()
    for m in PATTERN.finditer(text):
        line_no = text[: m.start()].count("\n") + 1
        offenders.append(f"{rel}:{line_no}")

if offenders:
    print("FAIL: pg_insert(nav_history) found outside portfolio_loader.py:", file=sys.stderr)
    for o in offenders:
        print(f"  - {o}", file=sys.stderr)
    sys.exit(1)
print("OK: only portfolio_loader.py uses pg_insert(nav_history).")
```

Wire into `.github/workflows/ci.yml` under the existing `python-tests` job (or alongside `order_path_caller_allowlist.py`):

```yaml
- name: nav_history writer guard
  run: uv run python scripts/checks/no_pg_insert_nav_history.py
```

- [ ] **Step 6: Run unification tests, confirm pass**

```bash
uv run pytest scripts/tests/test_nav_history_writer_unification.py -xvs
```

Expected: 5 passed.

- [ ] **Step 7: Regression sweep**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: no failures in `test_ib_sync_*`, `test_futu_nav_persistence*`, `test_portfolio_loader*`, `test_read_only_mode*`. The FUTU race-safe test in particular should still pass — its assertion about catching `IntegrityError → re-query → 409` is preserved by the app-level guard + the unique-PK constraint combination inside `upsert_nav_sync`.

- [ ] **Step 8: Commit**

```bash
git add src/xenon/db/migrations/versions/2026_06_03_nav_history_source_in_pk.py \
        src/xenon/db/schema.py \
        src/xenon/utils/portfolio_loader.py \
        src/xenon/execution/ib_sync.py \
        src/xenon/api/services/futu_nav_persistence.py \
        src/xenon/db/queries/portfolio.py \
        scripts/checks/no_pg_insert_nav_history.py \
        .github/workflows/ci.yml \
        scripts/tests/test_nav_history_writer_unification.py \
        scripts/tests/test_nav_history_source_pk_migration.py
git commit -m "refactor(nav): unify four writers + source in PK (Pass-2 E1-a)

Schema migration adds source to the PK and to nav_history_one_env_per_day
so intraday and close rows coexist as separate audit rows. upsert_nav_sync
becomes the single pg_insert(nav_history) caller; ib_sync, persist_futu_nav,
and db.queries.portfolio.upsert_nav all delegate. enforce_account_env_guard
is default-on (legacy unscoped callers use _upsert_nav_sync_unguarded).
upsert_nav_async wraps the same core for FastAPI callers (no event-loop
blocking). Race-safe IntegrityError catch + re-query winner hoisted from
the FUTU writer. CI guard prevents future divergence.

Pass-2 tribunal review applied: T1 (schema migration covers both PK and
secondary index), T3 (guard default-on), T4 (4th writer found by Codex),
T5 (full race-safe pattern preserved), T6 (async wrapper)."
```

---

## Task 1: Period resolver (pure function, no DB)

**Background:** Convert `period ∈ {"1M", "3M", "YTD", "All"}` to a concrete `period_start: date`. "All" needs inception, which the caller supplies (resolved from `min(nav_history.date)` for the scope). Pure function — no DB, no env — keeps it trivially testable and reusable. Default `period` value is `"YTD"` so existing callers without the param see no change.

**Files:**

- Create: `src/xenon/api/services/performance_periods.py`
- Test: `scripts/tests/test_performance_periods.py`

- [ ] **Step 1: Write the failing tests**

Create `scripts/tests/test_performance_periods.py`:

```python
"""Period resolver — pure function, no DB."""

from __future__ import annotations

from datetime import date

import pytest

from xenon.api.services.performance_periods import (
    SUPPORTED_PERIODS,
    InvalidPeriodError,
    resolve_period_start,
)


def test_ytd_returns_jan_1_of_as_of_year():
    assert resolve_period_start("YTD", as_of=date(2026, 6, 3), inception=date(2021, 1, 18)) == date(2026, 1, 1)


def test_one_month_returns_30_days_back():
    assert resolve_period_start("1M", as_of=date(2026, 6, 3), inception=date(2021, 1, 18)) == date(2026, 5, 4)


def test_three_month_returns_90_days_back():
    assert resolve_period_start("3M", as_of=date(2026, 6, 3), inception=date(2021, 1, 18)) == date(2026, 3, 5)


def test_all_returns_inception_when_inception_provided():
    assert resolve_period_start("All", as_of=date(2026, 6, 3), inception=date(2021, 1, 18)) == date(2021, 1, 18)


def test_all_falls_back_to_ytd_when_inception_missing():
    assert resolve_period_start("All", as_of=date(2026, 6, 3), inception=None) == date(2026, 1, 1)


def test_period_clamped_to_inception():
    """1M back from 2021-02-01 with inception 2021-01-18 → use inception, not 2021-01-02."""
    assert resolve_period_start("1M", as_of=date(2021, 2, 1), inception=date(2021, 1, 18)) == date(2021, 1, 18)


def test_invalid_period_raises():
    with pytest.raises(InvalidPeriodError):
        resolve_period_start("6M", as_of=date(2026, 6, 3), inception=None)


def test_supported_periods_constant():
    assert SUPPORTED_PERIODS == ("1M", "3M", "YTD", "All")


def test_case_normalization():
    """Accept lower/mixed case for API robustness."""
    assert resolve_period_start("ytd", as_of=date(2026, 6, 3), inception=None) == date(2026, 1, 1)
    assert resolve_period_start("All", as_of=date(2026, 6, 3), inception=date(2024, 1, 1)) == date(2024, 1, 1)
    assert resolve_period_start("all", as_of=date(2026, 6, 3), inception=date(2024, 1, 1)) == date(2024, 1, 1)
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
uv run pytest scripts/tests/test_performance_periods.py -xvs
```

Expected: All 8 fail with `ModuleNotFoundError: No module named 'xenon.api.services.performance_periods'`.

- [ ] **Step 3: Implement the resolver**

Create `src/xenon/api/services/performance_periods.py`:

```python
"""Period-string → date resolver for /performance.

Pure function — no DB, no env. The caller resolves inception separately
(typically min(nav_history.date) for the scope) and passes it in.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

SUPPORTED_PERIODS: tuple[str, ...] = ("1M", "3M", "YTD", "All")

# Case-insensitive lookup → canonical token.
_NORMALIZE = {p.lower(): p for p in SUPPORTED_PERIODS}


class InvalidPeriodError(ValueError):
    """Raised when ``period`` is not in SUPPORTED_PERIODS (case-insensitive)."""


def _normalize(period: str) -> str:
    key = period.strip().lower()
    if key not in _NORMALIZE:
        raise InvalidPeriodError(
            f"period must be one of {SUPPORTED_PERIODS!r}, got {period!r}"
        )
    return _NORMALIZE[key]


def resolve_period_start(
    period: str, *, as_of: date, inception: Optional[date]
) -> date:
    """Map a period token to a concrete start date.

    1M / 3M: 30 / 90 calendar days back from ``as_of`` (clamped to inception
    so we never look earlier than the scope's first NAV row).
    YTD: Jan 1 of ``as_of.year``.
    All: ``inception`` if known, else falls back to YTD (no NAV exists earlier
    anyway, but we want a safe default for cold-start scopes).
    """
    p = _normalize(period)
    if p == "YTD":
        candidate = date(as_of.year, 1, 1)
    elif p == "1M":
        candidate = as_of - timedelta(days=30)
    elif p == "3M":
        candidate = as_of - timedelta(days=90)
    elif p == "All":
        if inception is None:
            return date(as_of.year, 1, 1)
        return inception
    else:
        # Defensive — _normalize already raises on unknown tokens, but a
        # future maintainer adding to SUPPORTED_PERIODS will hit this.
        raise InvalidPeriodError(f"unhandled period: {p!r}")

    if inception is not None and candidate < inception:
        return inception
    return candidate
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
uv run pytest scripts/tests/test_performance_periods.py -xvs
```

Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/performance_periods.py scripts/tests/test_performance_periods.py
git commit -m "feat(perf): period resolver for /performance window

Pure function mapping {1M, 3M, YTD, All} to a period_start date.
Clamps to inception. Prereq for /performance?period= query param."
```

---

## Task 2: Return-formula library (Simple, TWR, IRR)

**Background:** Three formulas, no DB, no broker concept. `simple_flow_adjusted_return` matches retail intuition: `(end - start - net_flows) / start`. `time_weighted_return` chains daily returns where each daily return is `(NAV_t - NAV_{t-1} - flows_t) / NAV_{t-1}` — TWR isolates the manager's skill from cash-flow timing. `money_weighted_return_irr` solves the IRR equation `sum(flow_i / (1+r)^(t_i/365)) = 0` where flows include the opening NAV (positive outflow from investor's POV), interim deposits/withdrawals, and the closing NAV (negative — return of capital). Cash-flow sign convention: deposits / transfers-in are **positive** (money coming into the account); withdrawals / transfers-out are **negative**.

We use `scipy.optimize.brentq` for IRR — it's already a transitive dep via numpy/pandas. If scipy import fails we return `None` and surface a warning, rather than crashing the whole `/performance` payload.

**Files:**

- Create: `src/xenon/api/services/performance_returns.py`
- Test: `scripts/tests/test_performance_returns.py`

- [ ] **Step 1: Confirm scipy is available**

```bash
uv run python -c "from scipy.optimize import brentq; print('scipy ok')"
```

Expected: `scipy ok`. If it errors, add `scipy` to `pyproject.toml` `[project.dependencies]` before continuing. (Spot-check: `grep '^scipy' pyproject.toml` — it should already be present as a transitive of numpy/pandas-stack; if not, the import test catches it.)

- [ ] **Step 2: Write the failing tests**

Create `scripts/tests/test_performance_returns.py`:

```python
"""Pure return-formula tests — no DB, no DataFrame."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from xenon.api.services.performance_returns import (
    CashFlow,
    money_weighted_return_irr,
    simple_flow_adjusted_return,
    time_weighted_return,
)


def test_simple_no_flows():
    """100 → 110, no flows = +10%."""
    assert simple_flow_adjusted_return(start=100.0, end=110.0, net_flows=0.0) == pytest.approx(0.10)


def test_simple_with_deposit():
    """100 → 115, +5 deposit → real gain = 10, total_return = +10%."""
    assert simple_flow_adjusted_return(start=100.0, end=115.0, net_flows=5.0) == pytest.approx(0.10)


def test_simple_with_withdrawal():
    """100 → 95, -10 withdrawal → real gain = 5, total_return = +5%."""
    assert simple_flow_adjusted_return(start=100.0, end=95.0, net_flows=-10.0) == pytest.approx(0.05)


def test_simple_zero_start_returns_zero():
    """Cold-start protection: division by zero falls back to 0."""
    assert simple_flow_adjusted_return(start=0.0, end=100.0, net_flows=0.0) == 0.0


def test_twr_no_flows_matches_simple_compounding():
    """Daily returns +1%, +1%, +1% → (1.01)^3 - 1."""
    daily_returns = np.array([0.01, 0.01, 0.01])
    assert time_weighted_return(daily_returns) == pytest.approx((1.01 ** 3) - 1)


def test_twr_isolates_manager_skill_from_flow_timing():
    """Two scenarios with identical daily NAV-change returns yield identical TWR
    regardless of when an investor deposits — that's the point of TWR.
    """
    daily_returns = np.array([0.01, -0.005, 0.02])
    a = time_weighted_return(daily_returns)
    b = time_weighted_return(daily_returns)  # same input → same output
    assert a == b
    assert a == pytest.approx((1.01 * 0.995 * 1.02) - 1)


def test_twr_empty_returns_zero():
    assert time_weighted_return(np.array([])) == 0.0


def test_irr_no_flows_matches_compounding():
    """Single investment 100 at t0, withdrawal 110 at t=365 days → IRR ≈ 10%."""
    flows = [
        CashFlow(d=date(2025, 1, 1), amount=-100.0),  # invest 100
        CashFlow(d=date(2026, 1, 1), amount=110.0),   # withdraw 110
    ]
    irr = money_weighted_return_irr(flows)
    assert irr == pytest.approx(0.10, abs=1e-4)


def test_irr_with_intermediate_deposit():
    """Invest 100, deposit 50 at midpoint, end at 165 → solve numerically."""
    flows = [
        CashFlow(d=date(2025, 1, 1), amount=-100.0),
        CashFlow(d=date(2025, 7, 2), amount=-50.0),
        CashFlow(d=date(2026, 1, 1), amount=165.0),
    ]
    irr = money_weighted_return_irr(flows)
    # Verify by re-substituting into NPV equation
    t0 = date(2025, 1, 1)
    npv = sum(f.amount / ((1 + irr) ** ((f.d - t0).days / 365.25)) for f in flows)
    assert abs(npv) < 1e-3


def test_irr_returns_none_when_no_sign_change():
    """All flows positive (or all negative) → no root → return None."""
    flows = [
        CashFlow(d=date(2025, 1, 1), amount=100.0),
        CashFlow(d=date(2026, 1, 1), amount=110.0),
    ]
    assert money_weighted_return_irr(flows) is None


def test_irr_returns_none_for_too_few_flows():
    """Need at least 2 flows."""
    assert money_weighted_return_irr([CashFlow(d=date(2025, 1, 1), amount=-100.0)]) is None
    assert money_weighted_return_irr([]) is None
```

- [ ] **Step 3: Run tests, confirm they fail**

```bash
uv run pytest scripts/tests/test_performance_returns.py -xvs
```

Expected: All fail with `ModuleNotFoundError: No module named 'xenon.api.services.performance_returns'`.

- [ ] **Step 4: Implement the formulas**

Create `src/xenon/api/services/performance_returns.py`:

```python
"""Return-formula library — pure functions, no DB.

Three flavors, picked deliberately:

simple_flow_adjusted_return — retail-intuitive headline. Matches what most
brokers display: "my account is up $X, but $Y of that was deposits".

time_weighted_return — chains daily returns. The denominator each day is
yesterday's NAV; cash flows do NOT directly inflate the numerator (the
caller is responsible for subtracting flow_t in each daily_return). TWR
isolates the manager's compounding skill from the investor's flow timing.

money_weighted_return_irr — solves NPV = 0 for r, weighting cash flows by
when they occurred. Reflects the investor's actual experienced return.
For accounts with no interim flows, IRR ≈ Simple ≈ TWR.

Why all three? Different audiences. Retail users want Simple; finance
folks want TWR or IRR. The /performance tooltip surfaces all three so
the operator can pick the right number for the conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CashFlow:
    """Sign convention: positive = money INTO the account (investor's POV
    is outflow / cost basis); negative = money OUT (the closing NAV is
    represented as a final positive value because it returns to the
    investor). Concretely:

      opening NAV  → CashFlow(d=t0, amount=-NAV_open)   # investor "pays in"
      deposit      → CashFlow(d=t,  amount=-deposit)     # investor pays in
      withdrawal   → CashFlow(d=t,  amount=+withdrawal)  # investor receives
      closing NAV  → CashFlow(d=tN, amount=+NAV_close)   # account returns capital

    NOTE this is the IRR-side sign convention. The summary-level
    `net_external_flows` uses the opposite (deposits = +, withdrawals = -)
    because that's how the headline arithmetic reads. The caller is
    responsible for the conversion at the boundary.
    """

    d: date
    amount: float


def simple_flow_adjusted_return(*, start: float, end: float, net_flows: float) -> float:
    """`(end - start - net_flows) / start`.

    `net_flows` uses the retail convention: positive = deposit (money IN),
    negative = withdrawal (money OUT). Returns 0.0 when start <= 0 to avoid
    divide-by-zero on cold-start scopes.
    """
    if start <= 0:
        return 0.0
    return (end - start - net_flows) / start


def time_weighted_return(daily_returns: np.ndarray) -> float:
    """`∏(1 + r_i) - 1`. Empty input returns 0.0.

    Caller must compute daily_returns with cash flows already removed from
    the numerator: r_t = (NAV_t - NAV_{t-1} - flow_t) / NAV_{t-1}. See
    `_futu_returns` in performance.py for the calling convention.
    """
    if daily_returns is None or len(daily_returns) == 0:
        return 0.0
    return float(np.prod(1.0 + daily_returns) - 1.0)


def money_weighted_return_irr(flows: list[CashFlow]) -> Optional[float]:
    """Solve `Σ amount_i / (1+r)^(Δt_i / 365.25) = 0` for r.

    Returns None when:
      - fewer than 2 flows
      - all flows same sign (no NPV sign change → no root)
      - scipy is unavailable
      - brentq fails to converge inside [-0.999, 10.0]

    Day-count convention: 365.25 (handles leap years on average).
    """
    if flows is None or len(flows) < 2:
        return None

    signs = {1 if f.amount > 0 else -1 if f.amount < 0 else 0 for f in flows}
    signs.discard(0)
    if len(signs) < 2:
        # All positive or all negative → no IRR root.
        return None

    try:
        from scipy.optimize import brentq
    except ImportError:
        logger.warning("scipy unavailable — IRR not computed")
        return None

    t0 = flows[0].d

    def _npv(r: float) -> float:
        total = 0.0
        for f in flows:
            dt_years = (f.d - t0).days / 365.25
            total += f.amount / ((1.0 + r) ** dt_years)
        return total

    try:
        # Bracket wide: -99.9% (near-total loss) to +1000% annualized.
        return float(brentq(_npv, -0.999, 10.0, maxiter=200, xtol=1e-7))
    except (ValueError, RuntimeError) as exc:
        # brentq raises ValueError when bracket doesn't sandwich a root
        # (rare given the sign check above, but possible for pathological flows).
        logger.warning("IRR brentq failed: %s", exc)
        return None
```

- [ ] **Step 5: Run tests, confirm pass**

```bash
uv run pytest scripts/tests/test_performance_returns.py -xvs
```

Expected: `11 passed`.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/services/performance_returns.py scripts/tests/test_performance_returns.py
git commit -m "feat(perf): return-formula library (Simple/TWR/IRR)

Pure-function library for the three flavors that ship in the
/performance tooltip. Simple = headline (retail intuition);
TWR isolates manager skill from flow timing; IRR uses scipy
brentq with a wide bracket and falls back to None on failure."
```

---

## Task 3: FUTU cash-flow loader

**Background:** Bridge between `xenon.futu_cash_flow` rows (typed: `cashflow_type ∈ {DEPOSIT, WITHDRAW, TRANSFER_IN, TRANSFER_OUT}`, `amount`, `currency='USD'`, `occurred_at`) and the per-day `flow_t` series that `_futu_returns` consumes. Sign convention for `flow_t`: **positive = external deposit/transfer-in** (the NAV went up partly because money came in, not because of investment performance). This matches the daily-return formula `r_t = (NAV_t - NAV_{t-1} - flow_t) / NAV_{t-1}`.

`list_cashflows` already exists in `src/xenon/db/queries/futu_history.py` and returns `list[dict]` rows ordered by `occurred_at`. This loader wraps that, buckets by calendar date in the operator's session timezone (ET — same as `current_session_date_et`), and returns a `pd.Series[date, float]` so the caller can `.reindex(curve_dates, fill_value=0.0)` to align.

**Files:**

- Create: `src/xenon/api/services/performance_futu_flows.py`
- Test: `scripts/tests/test_performance_futu_flows.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_performance_futu_flows.py`:

```python
"""FUTU cash-flow loader — converts xenon.futu_cash_flow rows to per-day series."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from xenon.api.services.performance_futu_flows import load_futu_flows_per_day
from xenon.db.queries.futu_history import insert_cashflows
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.asyncio

_SCOPE = AccountScope(broker="FUTU", account_env="live", broker_account="123456")


async def _seed(engine, scope, rows: list[dict]) -> int:
    return await insert_cashflows(engine, scope, rows)


async def test_load_empty_returns_empty_series(async_engine):
    series = await load_futu_flows_per_day(
        async_engine, _SCOPE, since=date(2025, 1, 1), until=date(2025, 12, 31)
    )
    assert series.empty


async def test_deposit_positive_signed(async_engine):
    await _seed(async_engine, _SCOPE, [
        {
            "futu_flow_id": "f1",
            "cashflow_type": "DEPOSIT",
            "amount": Decimal("1000.00"),
            "currency": "USD",
            "occurred_at": datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc),
            "raw": {},
        },
    ])
    series = await load_futu_flows_per_day(
        async_engine, _SCOPE, since=date(2025, 1, 1), until=date(2025, 12, 31)
    )
    assert series.loc[date(2025, 3, 15)] == pytest.approx(1000.0)


async def test_withdrawal_negative_signed(async_engine):
    await _seed(async_engine, _SCOPE, [
        {
            "futu_flow_id": "f2",
            "cashflow_type": "WITHDRAW",
            "amount": Decimal("500.00"),
            "currency": "USD",
            "occurred_at": datetime(2025, 4, 1, 15, 0, tzinfo=timezone.utc),
            "raw": {},
        },
    ])
    series = await load_futu_flows_per_day(
        async_engine, _SCOPE, since=date(2025, 1, 1), until=date(2025, 12, 31)
    )
    assert series.loc[date(2025, 4, 1)] == pytest.approx(-500.0)


async def test_transfer_in_treated_as_deposit(async_engine):
    await _seed(async_engine, _SCOPE, [
        {
            "futu_flow_id": "f3",
            "cashflow_type": "TRANSFER_IN",
            "amount": Decimal("2500.00"),
            "currency": "USD",
            "occurred_at": datetime(2025, 5, 1, 14, 30, tzinfo=timezone.utc),
            "raw": {},
        },
    ])
    series = await load_futu_flows_per_day(
        async_engine, _SCOPE, since=date(2025, 1, 1), until=date(2025, 12, 31)
    )
    assert series.loc[date(2025, 5, 1)] == pytest.approx(2500.0)


async def test_multiple_same_day_summed(async_engine):
    """Deposit + withdrawal on same day → net result."""
    await _seed(async_engine, _SCOPE, [
        {
            "futu_flow_id": "f4a",
            "cashflow_type": "DEPOSIT",
            "amount": Decimal("1000.00"),
            "currency": "USD",
            "occurred_at": datetime(2025, 6, 1, 14, 0, tzinfo=timezone.utc),
            "raw": {},
        },
        {
            "futu_flow_id": "f4b",
            "cashflow_type": "WITHDRAW",
            "amount": Decimal("300.00"),
            "currency": "USD",
            "occurred_at": datetime(2025, 6, 1, 15, 0, tzinfo=timezone.utc),
            "raw": {},
        },
    ])
    series = await load_futu_flows_per_day(
        async_engine, _SCOPE, since=date(2025, 6, 1), until=date(2025, 6, 1)
    )
    assert series.loc[date(2025, 6, 1)] == pytest.approx(700.0)


async def test_scope_filter_excludes_other_account(async_engine):
    other = AccountScope(broker="FUTU", account_env="live", broker_account="999999")
    await _seed(async_engine, other, [
        {
            "futu_flow_id": "f5",
            "cashflow_type": "DEPOSIT",
            "amount": Decimal("9999.00"),
            "currency": "USD",
            "occurred_at": datetime(2025, 7, 1, 14, 0, tzinfo=timezone.utc),
            "raw": {},
        },
    ])
    series = await load_futu_flows_per_day(
        async_engine, _SCOPE, since=date(2025, 1, 1), until=date(2025, 12, 31)
    )
    assert series.empty
```

> **Fixture note:** if `async_engine` doesn't exist in `scripts/tests/conftest.py`, fall back to building one inline in the test via `from xenon.db.engine import get_engine` + a session-scope wrapper. Check first:
>
> ```bash
> grep -n "async_engine\|pg_test_engine" scripts/tests/conftest.py | head
> ```
>
> If only `pg_test_engine` (sync) exists, write an async fixture in this test file: open `get_engine()` once per session, depend on it via `pytestmark = pytest.mark.asyncio` and an autouse `await truncate_all_xenon_tables` at module scope.

- [ ] **Step 2: Run tests, confirm they fail**

```bash
uv run pytest scripts/tests/test_performance_futu_flows.py -xvs
```

Expected: All 6 fail with `ModuleNotFoundError: No module named 'xenon.api.services.performance_futu_flows'`.

- [ ] **Step 3: Implement the loader**

Create `src/xenon/api/services/performance_futu_flows.py`:

```python
"""FUTU cash-flow loader — bridges xenon.futu_cash_flow to per-day series.

Sign convention for the returned series (matches the daily-return formula
in performance.py::_futu_returns):

  positive = external deposit / transfer-in (NAV went up partly because
             money came in, not because of investment performance)
  negative = withdrawal / transfer-out (NAV went down partly because money
             left the account)

The caller subtracts this from the NAV delta to isolate investment-driven
return:  r_t = (NAV_t - NAV_{t-1} - flow_t) / NAV_{t-1}.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Mapping

import pandas as pd
import zoneinfo
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.queries.futu_history import list_cashflows
from xenon.execution.account_scope import AccountScope

# cashflow_type → sign multiplier.
# Deposits/transfers-in are positive (money INTO account).
# Withdrawals/transfers-out are negative.
_TYPE_SIGN: Mapping[str, int] = {
    "DEPOSIT": +1,
    "TRANSFER_IN": +1,
    "WITHDRAW": -1,
    "TRANSFER_OUT": -1,
}

# Statement boundaries are in ET (NYSE session timezone). A flow at 23:30 UTC
# on day D should land on calendar day D in ET if before midnight ET.
_ET = zoneinfo.ZoneInfo("America/New_York")


def _occurred_date_et(occurred_at: datetime) -> date:
    """Convert a tz-aware UTC timestamp into the calendar date in ET."""
    if occurred_at.tzinfo is None:
        # Defensive — schema column is TIMESTAMP WITH TIME ZONE so this shouldn't fire.
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return occurred_at.astimezone(_ET).date()


async def load_futu_flows_per_day(
    engine: AsyncEngine,
    scope: AccountScope,
    *,
    since: date,
    until: date,
) -> pd.Series:
    """Return pd.Series indexed by date (sorted, deduplicated), values are
    signed daily net external flow in USD.

    Empty index (no rows) returns an empty Series so callers can safely
    `.reindex(curve_dates, fill_value=0.0)`.
    """
    # list_cashflows takes datetime bounds, not date bounds. Convert the
    # inclusive date window to a half-open UTC datetime range:
    # [since 00:00 ET, until+1 day 00:00 ET).
    since_dt = datetime.combine(since, time.min).replace(tzinfo=_ET)
    until_dt = datetime.combine(until, time.max).replace(tzinfo=_ET)

    rows = await list_cashflows(engine, scope, since=since_dt, until=until_dt)
    if not rows:
        return pd.Series(dtype="float64")

    buckets: dict[date, float] = {}
    for row in rows:
        sign = _TYPE_SIGN.get(row["cashflow_type"])
        if sign is None:
            # Unknown cashflow_type — skip rather than crash.
            continue
        amount = row["amount"]
        if isinstance(amount, Decimal):
            amount = float(amount)
        else:
            amount = float(amount)
        d = _occurred_date_et(row["occurred_at"])
        buckets[d] = buckets.get(d, 0.0) + sign * amount

    if not buckets:
        return pd.Series(dtype="float64")

    series = pd.Series(buckets, dtype="float64")
    series.index = pd.Index(series.index, name="date")
    return series.sort_index()
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
uv run pytest scripts/tests/test_performance_futu_flows.py -xvs
```

Expected: `6 passed`. (If `async_engine` fixture wasn't present, the warning from Step 1's fixture note triggered fallback fixture work first.)

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/performance_futu_flows.py scripts/tests/test_performance_futu_flows.py
git commit -m "feat(perf): per-day FUTU cash-flow loader

Buckets xenon.futu_cash_flow by ET calendar date and applies the
sign convention performance.py needs (deposits +, withdrawals -).
Built on the existing list_cashflows query; no schema change."
```

---

## Task 4: Extend `load_nav_curve` with `period_end` + prefer-close + add inception query

**Background:** The current loader only takes `period_start`. We need a `period_end` for "All" so re-runs after market close don't include rows past today's session date (in ET). We also need a one-line query for the scope's earliest NAV row — that's how the period resolver gets `inception` when the API request asks for `period=All`.

**Pass-1 addition — prefer-close `DISTINCT ON`:** Once Task 11 ships, IB Flex starts writing `source='close'` rows daily. Under the current unique PK `(broker, account_env, broker_account, date)` only one row exists per date, but the close write _overwrites_ the intraday write (audit info lost from the row body). If E1 in Pass 2 picks option (a) (drop the unique PK), multiple rows coexist and prefer-close becomes the only way for the chart not to double-count.

The SQL is forward-compatible either way: `SELECT DISTINCT ON (date) ... ORDER BY date ASC, CASE source WHEN 'close' THEN 0 ELSE 1 END` returns one row per date today (no-op) and prefers close under option (a). Shipping it now avoids a schema-coupled refactor later.

**Files:**

- Modify: `src/xenon/db/queries/nav_history.py` (lines 32-61 — `load_nav_curve`)
- Add: `xenon.db.queries.nav_history.load_inception_date`
- Test: extend existing `scripts/tests/test_nav_history_queries.py` if present, else create `scripts/tests/test_nav_history_period_end.py`

- [ ] **Step 1: Locate the existing test file**

```bash
grep -rln "load_nav_curve\|load_inception" /Users/chenxi/projects/xenon/scripts/tests/ /Users/chenxi/projects/xenon/src/xenon/db/tests/ 2>/dev/null | head
```

Use the existing file if one matches; otherwise create `scripts/tests/test_nav_history_period_end.py` as below.

- [ ] **Step 2: Write the failing tests**

Add (or create) `scripts/tests/test_nav_history_period_end.py`:

```python
"""load_nav_curve period_end + load_inception_date."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from xenon.db.queries.nav_history import load_inception_date, load_nav_curve
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

pytestmark = pytest.mark.asyncio

_SCOPE = AccountScope(broker="FUTU", account_env="live", broker_account="555000")


def _seed(scope, days_navs: list[tuple[date, float]]) -> None:
    for d, nav in days_navs:
        upsert_nav_sync(scope=scope, day=d, nav=Decimal(str(nav)), source="close")


async def test_load_nav_curve_respects_period_end(async_engine):
    _seed(_SCOPE, [
        (date(2025, 1, 15), 100.0),
        (date(2025, 2, 15), 110.0),
        (date(2025, 3, 15), 105.0),
        (date(2025, 4, 15), 120.0),
    ])
    df = await load_nav_curve(
        async_engine, _SCOPE, period_start=date(2025, 1, 1), period_end=date(2025, 3, 31)
    )
    # Should include 1/15, 2/15, 3/15 but NOT 4/15.
    assert list(df["date"]) == [date(2025, 1, 15), date(2025, 2, 15), date(2025, 3, 15)]


async def test_load_nav_curve_period_end_none_keeps_old_behavior(async_engine):
    _seed(_SCOPE, [
        (date(2025, 1, 15), 100.0),
        (date(2025, 5, 15), 130.0),
    ])
    df = await load_nav_curve(
        async_engine, _SCOPE, period_start=date(2025, 1, 1), period_end=None
    )
    assert list(df["date"]) == [date(2025, 1, 15), date(2025, 5, 15)]


async def test_load_inception_returns_min_date(async_engine):
    _seed(_SCOPE, [
        (date(2024, 8, 1), 100.0),
        (date(2025, 1, 15), 110.0),
    ])
    inception = await load_inception_date(async_engine, _SCOPE)
    assert inception == date(2024, 8, 1)


async def test_load_inception_returns_none_when_no_rows(async_engine):
    empty_scope = AccountScope(broker="FUTU", account_env="live", broker_account="000000")
    inception = await load_inception_date(async_engine, empty_scope)
    assert inception is None


async def test_load_nav_curve_returns_intraday_when_only_intraday_exists(async_engine):
    """Pass-1: DISTINCT ON SQL is a no-op under the current unique PK."""
    upsert_nav_sync(scope=_SCOPE, day=date(2025, 9, 1), nav=Decimal("100.00"), source="intraday")
    upsert_nav_sync(scope=_SCOPE, day=date(2025, 9, 2), nav=Decimal("101.00"), source="intraday")
    df = await load_nav_curve(
        async_engine, _SCOPE, period_start=date(2025, 1, 1)
    )
    assert list(df["source"]) == ["intraday", "intraday"]
    assert list(df["nav"]) == [100.0, 101.0]


async def test_load_nav_curve_returns_close_when_only_close_exists(async_engine):
    """Same shape with source='close' — DISTINCT ON still returns one row per date."""
    upsert_nav_sync(scope=_SCOPE, day=date(2025, 10, 1), nav=Decimal("100.00"), source="close")
    upsert_nav_sync(scope=_SCOPE, day=date(2025, 10, 2), nav=Decimal("101.00"), source="close")
    df = await load_nav_curve(
        async_engine, _SCOPE, period_start=date(2025, 1, 1)
    )
    assert list(df["source"]) == ["close", "close"]


async def test_load_nav_curve_mixed_sources_across_dates(async_engine):
    """Different dates, different sources — all returned, ordered by date."""
    upsert_nav_sync(scope=_SCOPE, day=date(2025, 11, 1), nav=Decimal("100.00"), source="intraday")
    upsert_nav_sync(scope=_SCOPE, day=date(2025, 11, 2), nav=Decimal("101.00"), source="close")
    upsert_nav_sync(scope=_SCOPE, day=date(2025, 11, 3), nav=Decimal("102.00"), source="intraday")
    df = await load_nav_curve(
        async_engine, _SCOPE, period_start=date(2025, 1, 1)
    )
    assert list(df["date"]) == [date(2025, 11, 1), date(2025, 11, 2), date(2025, 11, 3)]
    assert list(df["source"]) == ["intraday", "close", "intraday"]


# NOTE: The two-rows-same-date prefer-close test is deferred until Pass 2
# resolves E1. Under the current unique PK on (broker, account_env,
# broker_account, date), two rows cannot coexist. If Pass 2 picks
# option (a) (drop the unique PK), add a test here that seeds an
# intraday row + close row for the same date via raw INSERT and asserts
# the close row wins. Under option (b) (intraday no-op when close
# exists), no additional test is needed beyond the no-op behavior pinned
# at the writer level.
```

- [ ] **Step 3: Run tests, confirm they fail**

```bash
uv run pytest scripts/tests/test_nav_history_period_end.py -xvs
```

Expected: First two fail with `TypeError: load_nav_curve() got an unexpected keyword argument 'period_end'`; last two fail with `ImportError: cannot import name 'load_inception_date'`.

- [ ] **Step 4: Implement the changes**

Edit `src/xenon/db/queries/nav_history.py`. Replace the `load_nav_curve` function (currently lines 32-61) with:

```python
async def load_nav_curve(
    engine: AsyncEngine,
    scope: AccountScope,
    period_start: date,
    period_end: date | None = None,
) -> pd.DataFrame:
    """Return DataFrame[date, nav, daily_pnl, source] ascending by date,
    scope-filtered, optionally upper-bounded by period_end (inclusive).

    Prefer-close semantics: when two rows exist for the same date with
    different ``source`` values, the ``close`` row wins. Today the unique
    PK prevents that case; the DISTINCT ON keeps the query forward-
    compatible with E1-option-(a) (multiple rows per date with source
    joining the key) without re-touching this code.
    """
    where = (
        (nav_history.c.broker == scope.broker)
        & (nav_history.c.account_env == scope.account_env)
        & (nav_history.c.broker_account == scope.broker_account)
        & (nav_history.c.date >= period_start)
    )
    if period_end is not None:
        where = where & (nav_history.c.date <= period_end)

    # DISTINCT ON (date) — PG-specific. ORDER BY date ASC for the outer
    # iteration, then source ranked so 'close' (rank=0) wins over
    # 'intraday' (rank=1) when both rows exist for the same date.
    source_rank = sa.case(
        (nav_history.c.source == "close", 0),
        else_=1,
    ).label("_source_rank")

    inner = (
        sa.select(
            nav_history.c.date,
            nav_history.c.nav,
            nav_history.c.daily_pnl,
            nav_history.c.source,
            source_rank,
        )
        .where(where)
        .distinct(nav_history.c.date)
        .order_by(nav_history.c.date.asc(), source_rank.asc())
    )

    async with engine.begin() as conn:
        result = await conn.execute(inner)
        rows = result.fetchall()
    df = pd.DataFrame(rows, columns=["date", "nav", "daily_pnl", "source", "_source_rank"])
    df = df.drop(columns=["_source_rank"])
    if not df.empty:
        df["nav"] = df["nav"].astype(float)
        df["daily_pnl"] = df["daily_pnl"].astype(float).where(df["daily_pnl"].notna(), None)
    return df
```

Then add the `load_inception_date` helper just below `load_nav_curve` (and above `load_benchmark_cached`):

```python
async def load_inception_date(
    engine: AsyncEngine, scope: AccountScope
) -> date | None:
    """Return the earliest nav_history.date for the scope, or None if no rows.

    Used by the /performance route to resolve period=All to a concrete
    start date. Cheap (PK-prefix indexed) so safe to call on every request.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            sa.select(sa.func.min(nav_history.c.date))
            .where(
                (nav_history.c.broker == scope.broker)
                & (nav_history.c.account_env == scope.account_env)
                & (nav_history.c.broker_account == scope.broker_account)
            )
        )
        row = result.first()
    if row is None or row[0] is None:
        return None
    return row[0]
```

- [ ] **Step 5: Run tests, confirm pass**

```bash
uv run pytest scripts/tests/test_nav_history_period_end.py -xvs
```

Expected: `4 passed`.

- [ ] **Step 6: Regression sweep — existing nav_history callers**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: no failures. `load_nav_curve(engine, scope, period_start)` still works (the new `period_end` parameter defaults to `None`).

- [ ] **Step 7: Commit**

```bash
git add src/xenon/db/queries/nav_history.py scripts/tests/test_nav_history_period_end.py
git commit -m "feat(nav): load_nav_curve period_end + load_inception_date

period_end (optional, inclusive) lets the /performance route bound
the window — required for the 1M/3M filters. load_inception_date
resolves period=All to the earliest row for the scope."
```

---

## Task 5: Wire `period` through `compute()`, cache, and route

**Background:** The bulk of the back-end change. `compute()` accepts `period`, resolves it via Task 1's helper using inception from Task 4, calls `load_nav_curve(period_start, period_end=as_of)` to get the windowed curve, computes the four return flavors for FUTU (Simple as headline, TWR / IRR / net_external_flows in summary), and lifts the FUTU mask gate. IB keeps its current behavior except summary[`simple_total_return`] is filled with the existing `total_return` value (deposits unknown → equal to gross) and the other three stay `None`.

The cache key must include `period` — otherwise switching from `YTD` to `3M` returns the cached YTD result.

This task batches changes across four files because they're tightly coupled (route → cache → compute → response shape); separating risks intermediate broken states.

**Files:**

- Modify: `src/xenon/api/services/performance.py`
- Modify: `src/xenon/api/services/perf_cache.py`
- Modify: `src/xenon/api/routes/performance.py`
- Test: `scripts/tests/test_performance_period_e2e.py` (create)
- Test: `scripts/tests/test_performance_futu_unmasked.py` (create)

- [ ] **Step 1: Write the failing integration tests**

Create `scripts/tests/test_performance_period_e2e.py`:

```python
"""compute() with the period parameter — end-to-end against PG."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from xenon.api.services.perf_cache import cached_compute, clear_cache
from xenon.api.services.performance import compute
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

pytestmark = pytest.mark.asyncio

_SCOPE = AccountScope(broker="IB", account_env="live", broker_account="U18007831")


def _seed_nav(scope, navs: list[tuple[date, float]]):
    for d, n in navs:
        upsert_nav_sync(scope=scope, day=d, nav=Decimal(str(n)), source="close")


async def test_period_ytd_default(async_engine):
    _seed_nav(_SCOPE, [
        (date(2025, 12, 1), 100.0),
        (date(2026, 1, 2), 101.0),
        (date(2026, 6, 1), 110.0),
    ])
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3))
    # Default = YTD → window is [2026-01-01, 2026-06-03]. Curve contains 1/2 and 6/1
    # (12/1 is pre-Jan and excluded). period_start follows Task 1's contract (Jan 1),
    # NOT the first observed NAV date (Pass-2 T9 fix — was "2026-01-02").
    assert result["status"] == "ok"
    assert result["period_start"] == "2026-01-01"
    assert len(result["series"]) == 2


async def test_period_1m_narrows_to_30_days(async_engine):
    _seed_nav(_SCOPE, [
        (date(2026, 1, 2), 100.0),
        (date(2026, 4, 1), 105.0),
        (date(2026, 5, 30), 108.0),
        (date(2026, 6, 1), 110.0),
    ])
    result = await compute(
        async_engine, _SCOPE, as_of=date(2026, 6, 3), period="1M"
    )
    # 1M back from 2026-06-03 = 2026-05-04 — only 5/30 and 6/1 qualify.
    assert result["status"] == "ok"
    assert len(result["series"]) == 2
    assert result["period_start"] == "2026-05-04"


async def test_period_all_uses_inception(async_engine):
    _seed_nav(_SCOPE, [
        (date(2024, 8, 1), 100.0),
        (date(2026, 6, 1), 200.0),
    ])
    result = await compute(
        async_engine, _SCOPE, as_of=date(2026, 6, 3), period="All"
    )
    assert result["status"] == "ok"
    assert len(result["series"]) == 2
    assert result["period_start"] == "2024-08-01"


async def test_invalid_period_raises(async_engine):
    from xenon.api.services.performance_periods import InvalidPeriodError
    _seed_nav(_SCOPE, [(date(2026, 1, 2), 100.0)])
    with pytest.raises(InvalidPeriodError):
        await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3), period="6M")


async def test_cache_key_includes_period(async_engine):
    """Two period values must NOT share a cache entry."""
    clear_cache()
    _seed_nav(_SCOPE, [
        (date(2026, 1, 2), 100.0),
        (date(2026, 5, 30), 105.0),
        (date(2026, 6, 1), 110.0),
    ])
    ytd = await cached_compute(async_engine, _SCOPE, period="YTD")
    m1 = await cached_compute(async_engine, _SCOPE, period="1M")
    assert len(ytd["series"]) >= len(m1["series"])
    # Two distinct results — proves cache didn't return YTD for 1M.
    assert ytd["period_start"] != m1["period_start"]


async def test_summary_includes_new_return_fields(async_engine):
    _seed_nav(_SCOPE, [
        (date(2026, 1, 2), 100.0),
        (date(2026, 6, 1), 110.0),
    ])
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3))
    summary = result["summary"]
    # All four new fields present (IB without flows → 3 of them are None / equal-to-simple).
    assert "simple_total_return" in summary
    assert "twr_total_return" in summary
    assert "irr_total_return" in summary
    assert "net_external_flows" in summary
    # IB has no flow source → simple equals total_return.
    assert summary["simple_total_return"] == pytest.approx(summary["total_return"])
    assert summary["net_external_flows"] == 0.0
```

Create `scripts/tests/test_performance_futu_unmasked.py`:

```python
"""FUTU mask lifts when cash flows are integrated."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from xenon.api.services.performance import compute
from xenon.db.queries.futu_history import insert_cashflows
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

pytestmark = pytest.mark.asyncio

_SCOPE = AccountScope(broker="FUTU", account_env="live", broker_account="777111")


def _seed_nav(scope, navs: list[tuple[date, float]]):
    for d, n in navs:
        upsert_nav_sync(scope=scope, day=d, nav=Decimal(str(n)), source="close")


def _build_50_day_nav(start_d: date, start_nav: float) -> list[tuple[date, float]]:
    """50 alternating up/down days for a stable test fixture."""
    from datetime import timedelta
    out, n = [], start_nav
    for i in range(50):
        n = n * (1.01 if i % 2 == 0 else 0.995)
        out.append((start_d + timedelta(days=i), n))
    return out


async def test_futu_warning_replaced_when_unmasked(async_engine):
    _seed_nav(_SCOPE, _build_50_day_nav(date(2026, 3, 1), 100_000.0))
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3))
    warnings_text = " ".join(result["warnings"])
    # Old masking warning must NOT appear (we lifted the gate).
    assert "True Time-Weighted Return requires cash-flow tracking" not in warnings_text


async def test_futu_risk_metrics_populated(async_engine):
    _seed_nav(_SCOPE, _build_50_day_nav(date(2026, 3, 1), 100_000.0))
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3))
    summary = result["summary"]
    # Was None pre-change; should now be a float.
    assert summary["sharpe_ratio"] is not None
    assert summary["sortino_ratio"] is not None
    assert summary["annualized_return"] is not None


async def test_futu_deposit_excluded_from_simple_total_return(async_engine):
    """Deposit 5000 mid-period. End - start = 8000, but real gain is 3000 = +3%."""
    _seed_nav(_SCOPE, [
        (date(2026, 1, 2), 100_000.0),
        (date(2026, 3, 15), 102_500.0),
        (date(2026, 6, 1), 108_000.0),
    ])
    await insert_cashflows(async_engine, _SCOPE, [
        {
            "futu_flow_id": "dep1",
            "cashflow_type": "DEPOSIT",
            "amount": Decimal("5000.00"),
            "currency": "USD",
            "occurred_at": datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc),
            "raw": {},
        },
    ])
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3))
    summary = result["summary"]
    assert summary["net_external_flows"] == pytest.approx(5000.0)
    assert summary["simple_total_return"] == pytest.approx(0.03, abs=1e-4)
    # twr / irr are populated but their exact values aren't pinned here.
    assert summary["twr_total_return"] is not None
    assert summary["irr_total_return"] is not None
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
uv run pytest scripts/tests/test_performance_period_e2e.py scripts/tests/test_performance_futu_unmasked.py -xvs
```

Expected: Period-e2e tests fail with `TypeError: compute() got an unexpected keyword argument 'period'`. FUTU-unmasked tests fail on `assert "True Time-Weighted Return ..." not in warnings_text` (warning is still there) or on `summary["sharpe_ratio"] is not None` (still masked).

- [ ] **Step 3: Modify `compute()` to accept `period`**

Edit `src/xenon/api/services/performance.py`. Replace the function signature + initial body. Locate:

```python
async def compute(
    engine: AsyncEngine, scope: AccountScope, *, ib_pool=None, as_of: date | None = None,
) -> dict[str, Any]:
    """Build the PerformanceData dict for one (broker, account_env, broker_account)."""
    period_start = _period_start(as_of)
    curve = await load_nav_curve(engine, scope, period_start)
```

Replace those four lines (signature + first two body lines) with:

```python
async def compute(
    engine: AsyncEngine,
    scope: AccountScope,
    *,
    ib_pool=None,
    as_of: date | None = None,
    period: str = "YTD",
) -> dict[str, Any]:
    """Build the PerformanceData dict for one (broker, account_env, broker_account).

    ``period`` ∈ {"1M", "3M", "YTD", "All"}; case-insensitive. Default "YTD"
    preserves prior behavior. "All" reads the scope's inception from
    nav_history. Raises InvalidPeriodError on unknown values.
    """
    from xenon.api.services.performance_periods import resolve_period_start
    from xenon.db.queries.nav_history import load_inception_date

    as_of_d = as_of or current_session_date_et()
    inception = await load_inception_date(engine, scope)
    period_start = resolve_period_start(period, as_of=as_of_d, inception=inception)
    curve = await load_nav_curve(engine, scope, period_start, period_end=as_of_d)
```

- [ ] **Step 4: Add the new summary fields + populate from FUTU cash flows**

Still in `performance.py`. Locate the `_base_summary` function and add the four new fields at the end (after the `for k in ANNUALIZED_RISK_FIELDS + ...` loop but before `return out`):

```python
    # PR-2: honest-% surface. None until populated by broker-specific branch
    # below. simple_total_return mirrors total_return when no flows exist;
    # the others stay None for IB until Flex CashTransaction support lands.
    out["simple_total_return"] = None
    out["twr_total_return"] = None
    out["irr_total_return"] = None
    out["net_external_flows"] = None
    return out
```

Then locate the broker-branch block:

```python
    if scope.broker == "IB":
        returns = _ib_returns(curve)
    else:
        returns = _futu_returns(curve)
```

Replace it with the flow-aware version:

```python
    if scope.broker == "IB":
        returns = _ib_returns(curve)
        flows_per_day = None
    else:
        # FUTU branch: load cash flows for the window and adjust daily returns
        # so they isolate investment-driven performance from external flows.
        from xenon.api.services.performance_futu_flows import load_futu_flows_per_day

        flows_per_day = await load_futu_flows_per_day(
            engine, scope, since=period_start, until=as_of_d
        )
        returns = _futu_returns(curve, flows_per_day)
```

Update `_futu_returns` to accept the optional flow series. Locate the existing definition (around line 212) and replace it entirely with:

```python
def _futu_returns(
    curve: pd.DataFrame, flows_per_day: pd.Series | None = None
) -> np.ndarray:
    """Flow-adjusted daily return: r_t = (NAV_t - NAV_{t-1} - flow_t) / NAV_{t-1}.

    flows_per_day is signed (deposit = +, withdrawal = -). Missing dates are
    filled with 0. returns[0] = 0 by convention (no prior NAV to chain from).
    """
    nav = curve["nav"].astype(float).to_numpy()
    if len(nav) < 1:
        return np.array([])
    prev = np.concatenate(([nav[0]], nav[:-1]))

    if flows_per_day is not None and not flows_per_day.empty:
        flows_aligned = flows_per_day.reindex(curve["date"], fill_value=0.0).to_numpy(dtype=float)
    else:
        flows_aligned = np.zeros(len(nav), dtype=float)

    returns = np.where(prev > 0, (nav - prev - flows_aligned) / prev, 0.0)
    returns[0] = 0.0
    return returns
```

Now add a small helper to build the four summary fields and populate it inside `compute()`. Add this helper function before `compute()` (e.g. just after `_bench_total_return`):

```python
def _fill_return_flavors(
    summary: dict,
    curve: pd.DataFrame,
    returns: np.ndarray,
    flows_per_day: pd.Series | None,
) -> None:
    """Populate the four PR-2 honest-% fields on the summary dict.

    IB (flows_per_day is None): simple == total_return (no deposits known),
    net_external_flows = 0.0, twr / irr stay None.

    FUTU: all four populated. Simple uses retail sign convention (deposits +).
    IRR uses CashFlow's investor-POV sign convention (deposits −, withdrawals +,
    closing NAV +, opening NAV −).
    """
    from xenon.api.services.performance_returns import (
        CashFlow,
        money_weighted_return_irr,
        simple_flow_adjusted_return,
        time_weighted_return,
    )

    if len(curve) == 0:
        return

    start_nav = float(curve["nav"].iloc[0])
    end_nav = float(curve["nav"].iloc[-1])
    if flows_per_day is None or flows_per_day.empty:
        net_flows = 0.0
        irr = None
        twr = None
    else:
        net_flows = float(flows_per_day.sum())
        # TWR uses the same flow-adjusted daily returns we already computed.
        # Skip returns[0] (synthetic zero) so single-day curves still produce 0.
        twr = time_weighted_return(returns[1:] if len(returns) > 1 else returns)
        # IRR flows: opening NAV at curve[0], one row per flow day, closing
        # NAV at curve[-1]. Sign convention follows CashFlow's docstring.
        irr_flows = [CashFlow(d=curve["date"].iloc[0], amount=-start_nav)]
        for d, amt in flows_per_day.items():
            # retail-sign amt (deposit = +) → investor-POV (deposit = -).
            irr_flows.append(CashFlow(d=d, amount=-float(amt)))
        irr_flows.append(CashFlow(d=curve["date"].iloc[-1], amount=end_nav))
        irr = money_weighted_return_irr(irr_flows)

    summary["simple_total_return"] = simple_flow_adjusted_return(
        start=start_nav, end=end_nav, net_flows=net_flows
    )
    summary["twr_total_return"] = twr
    summary["irr_total_return"] = irr
    summary["net_external_flows"] = net_flows
```

Wire it into `compute()`. Find the line `summary = _base_summary(nav, days_collected)` and add right after it:

```python
    _fill_return_flavors(summary, curve, returns, flows_per_day)
```

Now lift the FUTU mask. Locate:

```python
    futu_mask = scope.broker == "FUTU"
    ib_mask = scope.broker == "IB" and _ib_should_mask_metrics()
    if ib_mask:
        warnings.append(
            "IB TWR requires cash-flow tracking — follow-up. "
            "See docs/superpowers/reports/2026-06-01-ib-dailypnl-verification.md."
        )
    if futu_mask:
        warnings.append(
            "FUTU NAV-change returns include external cash flows "
            "(deposits, withdrawals, dividends). True Time-Weighted Return "
            "requires cash-flow tracking — follow-up."
        )
```

Replace with:

```python
    futu_mask = False  # PR-2: lifted now that flow-adjusted returns + IRR are computed
    ib_mask = scope.broker == "IB" and _ib_should_mask_metrics()
    if ib_mask:
        warnings.append(
            "IB TWR requires cash-flow tracking — follow-up. "
            "See docs/superpowers/reports/2026-06-01-ib-dailypnl-verification.md."
        )
    if scope.broker == "FUTU":
        # Soft note — risk metrics now ship, but operator should know the
        # backbone is cash-flow tracking via xenon.futu_cash_flow.
        warnings.append(
            "FUTU returns are flow-adjusted via xenon.futu_cash_flow. "
            "Simple total return uses retail convention; TWR / IRR also shown."
        )
```

- [ ] **Step 5: Update `perf_cache` cache key to include period**

Edit `src/xenon/api/services/perf_cache.py`. Replace the `_key` function:

```python
def _key(scope: AccountScope, period: str = "YTD") -> tuple[str, str, str, str]:
    return (scope.broker, scope.account_env, scope.broker_account, period.lower())
```

Replace `cached_compute`:

```python
async def cached_compute(
    engine, scope: AccountScope, *, ib_pool=None, period: str = "YTD"
) -> Any:
    """Memoized wrapper around `xenon.api.services.performance.compute`.

    The cache key includes (broker, account_env, broker_account, period) —
    period switches return live results, not stale YTD.
    """
    from xenon.api.services.performance import compute as _inner

    k = _key(scope, period)
    ttl = _ttl_for_now()
    now = time.time()
    cached = _cache.get(k)
    if cached is not None and (now - cached[1]) < ttl:
        return cached[0]
    result = await _inner(engine, scope, ib_pool=ib_pool, period=period)
    _cache[k] = (result, now)
    return result
```

Also update the type annotation on `_cache` at the top of the file:

```python
_cache: dict[tuple[str, str, str, str], tuple[Any, float]] = {}
```

(Old: `tuple[str, str, str]`.)

And update `warm()` analogously — locate and replace:

```python
def warm(engine, scope: AccountScope, *, ib_pool=None, period: str = "YTD") -> None:
    """Fire-and-forget warmup. Used by deprecated POST /performance/background."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(cached_compute(engine, scope, ib_pool=ib_pool, period=period))
```

- [ ] **Step 6: Update the route to accept `?period=`**

Edit `src/xenon/api/routes/performance.py`. Replace with:

```python
"""GET /performance — broker-aware, scope-keyed, market-aware-TTL cached.

Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md (v3.1)
Service: xenon.api.services.performance.compute (via perf_cache.cached_compute)
Dep: xenon.api.guards.get_performance_scope (broker-aware)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from xenon.api.guards import get_performance_scope
from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict
from xenon.api.services.perf_cache import cached_compute
from xenon.api.services.performance_periods import (
    SUPPORTED_PERIODS,
    InvalidPeriodError,
)
from xenon.execution.account_scope import AccountScope

router = APIRouter()


@router.get("/performance")
async def get_performance(
    request: Request,
    scope: AccountScope = Depends(get_performance_scope),
    period: str = Query(
        "YTD",
        description=f"Window. One of {SUPPORTED_PERIODS} (case-insensitive).",
    ),
):
    """Return the performance payload for the resolved broker scope + window."""
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="db engine not initialized")
    ib_pool = getattr(request.app.state, "ib_pool", None)
    try:
        return await cached_compute(engine, scope, ib_pool=ib_pool, period=period)
    except InvalidPeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NavAccountEnvConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
```

- [ ] **Step 7: Run tests, confirm pass**

```bash
uv run pytest scripts/tests/test_performance_period_e2e.py scripts/tests/test_performance_futu_unmasked.py -xvs
```

Expected: all 9 pass. If `test_summary_includes_new_return_fields` fails because `simple_total_return == total_return` differs slightly, check that `_fill_return_flavors` is being called BEFORE `_fill_annualized` (which doesn't touch the new fields anyway, but order matters if you accidentally overwrite).

- [ ] **Step 8: Regression sweep — existing /performance behavior preserved**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: existing tests under `src/xenon/api/services/tests/test_perf_cache.py`, `scripts/tests/test_performance_*.py`, and any router tests pass. Two scenarios worth a manual eyeball:

- A test that didn't pass `period` previously must still pass (default = "YTD").
- A test that depends on the warnings list for FUTU may need a string update (replace `"True Time-Weighted Return requires"` with `"flow-adjusted via xenon.futu_cash_flow"`).

If regression failures appear: update the impacted test's expected-string fragments rather than reverting the backend change — the new warning string is the durable behavior.

- [ ] **Step 9: Commit**

```bash
git add src/xenon/api/services/performance.py \
        src/xenon/api/services/perf_cache.py \
        src/xenon/api/routes/performance.py \
        scripts/tests/test_performance_period_e2e.py \
        scripts/tests/test_performance_futu_unmasked.py
git commit -m "feat(perf): period query param + FUTU flow-adjusted returns

GET /performance now accepts ?period=1M|3M|YTD|All. compute() resolves
the window, calls load_nav_curve with period_end clamping, and (for
FUTU) joins xenon.futu_cash_flow per-day to compute flow-adjusted
daily returns. Summary gains simple_total_return / twr_total_return /
irr_total_return / net_external_flows. FUTU risk metrics no longer
masked — Sharpe/Sortino/etc. ship now that returns are flow-adjusted.
Cache key includes period so window switches return live results."
```

---

## Task 6: Frontend — period selector component + types

**Background:** Frontend half kicks off with the period state. `PerformancePeriodSelector` is a small focused component (4 buttons, current selection styled active). The hook `usePerformance` accepts `period` and uses it in the endpoint URL — `useSyncHook`'s internal cache is already keyed by endpoint string, so adding `&period=` to the URL gives us per-period cache entries for free.

**Files:**

- Create: `web/components/PerformancePeriodSelector.tsx`
- Modify: `web/lib/types.ts` — add four summary fields + `PerformancePeriod` type
- Modify: `web/lib/usePerformance.ts` — accept `period` arg
- Modify: `web/app/api/performance/route.ts` — forward `?period=`
- Test: `web/tests/performance-period-selector.test.ts` (create)
- Test: `web/tests/use-performance-period.test.ts` (create)

- [ ] **Step 1: Read `web/lib/types.ts` and identify the summary type to extend**

```bash
grep -n "PerformanceSummary\|simple_total_return\|total_return" /Users/chenxi/projects/xenon/web/lib/types.ts | head -20
```

Locate the `summary` field of `PerformanceOk` (likely declared inline or via a `PerformanceSummary` type alias). Note the exact name — the next step depends on it.

- [ ] **Step 2: Write the failing tests**

Create `web/tests/performance-period-selector.test.ts`:

```typescript
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import PerformancePeriodSelector from "@/components/PerformancePeriodSelector";

describe("PerformancePeriodSelector", () => {
  it("renders 4 buttons in fixed order", () => {
    render(<PerformancePeriodSelector value="YTD" onChange={() => {}} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["1M", "3M", "YTD", "All"]);
  });

  it("marks the selected button as active via aria-pressed", () => {
    render(<PerformancePeriodSelector value="3M" onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "3M" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "YTD" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("calls onChange with the clicked period", () => {
    const onChange = vi.fn();
    render(<PerformancePeriodSelector value="YTD" onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "1M" }));
    expect(onChange).toHaveBeenCalledWith("1M");
  });

  it("does not re-fire onChange when clicking the active period", () => {
    const onChange = vi.fn();
    render(<PerformancePeriodSelector value="YTD" onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "YTD" }));
    expect(onChange).not.toHaveBeenCalled();
  });
});
```

Create `web/tests/use-performance-period.test.ts`:

```typescript
import { describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { usePerformance } from "@/lib/usePerformance";

// usePerformance internally calls useSyncHook with `endpoint`; we don't
// re-test useSyncHook itself, just that the endpoint URL embeds the period.
vi.mock("@/lib/useSyncHook", () => ({
  useSyncHook: vi.fn(() => ({
    data: null,
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  })),
}));

import { useSyncHook } from "@/lib/useSyncHook";

describe("usePerformance period parameter", () => {
  it("includes ?broker= and &period= in the endpoint", () => {
    renderHook(() => usePerformance(true, "FUTU", "3M"));
    const config = (useSyncHook as ReturnType<typeof vi.fn>).mock.calls.at(
      -1,
    )![0];
    expect(config.endpoint).toBe("/api/performance?broker=FUTU&period=3M");
  });

  it("defaults to broker=IB and period=YTD when args omitted", () => {
    renderHook(() => usePerformance(true));
    const config = (useSyncHook as ReturnType<typeof vi.fn>).mock.calls.at(
      -1,
    )![0];
    expect(config.endpoint).toBe("/api/performance?broker=IB&period=YTD");
  });

  it("changing the period creates a different config (per-period cache)", () => {
    const { rerender } = renderHook(
      ({ p }: { p: "1M" | "YTD" }) => usePerformance(true, "IB", p),
      { initialProps: { p: "YTD" } },
    );
    const ytdEndpoint = (useSyncHook as ReturnType<typeof vi.fn>).mock.calls.at(
      -1,
    )![0].endpoint;
    rerender({ p: "1M" });
    const oneMonthEndpoint = (
      useSyncHook as ReturnType<typeof vi.fn>
    ).mock.calls.at(-1)![0].endpoint;
    expect(ytdEndpoint).not.toBe(oneMonthEndpoint);
  });
});
```

- [ ] **Step 3: Run tests, confirm they fail**

```bash
cd web && npm test -- performance-period-selector use-performance-period
```

Expected: Selector tests fail with `Cannot find module '@/components/PerformancePeriodSelector'`. Hook tests fail because `usePerformance` ignores the third arg.

- [ ] **Step 4: Add types**

Edit `web/lib/types.ts`. Locate the summary type definition (likely a field on `PerformanceOk` or a `PerformanceSummary` alias). Inside its object literal, immediately above the closing `}`, add:

```typescript
/** PR-2: retail-intuitive headline. `(end - start - net_external_flows) / start`. */
simple_total_return: number | null;
/** PR-2: time-weighted return. `∏(1 + r_i) - 1` over flow-adjusted daily returns. */
twr_total_return: number | null;
/** PR-2: money-weighted IRR. `null` when scipy unavailable, no sign change, or convergence fails. */
irr_total_return: number | null;
/** PR-2: signed sum of cash flows in the window. Positive = net deposit. */
net_external_flows: number | null;
```

At the bottom of `web/lib/types.ts` (or wherever shared enum types live), add:

```typescript
export type PerformancePeriod = "1M" | "3M" | "YTD" | "All";

export const PERFORMANCE_PERIODS: readonly PerformancePeriod[] = [
  "1M",
  "3M",
  "YTD",
  "All",
] as const;
```

- [ ] **Step 5: Implement `PerformancePeriodSelector`**

Create `web/components/PerformancePeriodSelector.tsx`:

```typescript
"use client";

import {
  PERFORMANCE_PERIODS,
  type PerformancePeriod,
} from "@/lib/types";

type Props = {
  value: PerformancePeriod;
  onChange: (next: PerformancePeriod) => void;
};

export default function PerformancePeriodSelector({ value, onChange }: Props) {
  return (
    <div
      className="performance-period-selector"
      role="group"
      aria-label="Performance window"
      data-testid="performance-period-selector"
    >
      {PERFORMANCE_PERIODS.map((p) => {
        const active = p === value;
        return (
          <button
            key={p}
            type="button"
            className={`pill ${active ? "active" : ""}`}
            aria-pressed={active}
            data-testid={`performance-period-${p}`}
            onClick={() => {
              if (!active) onChange(p);
            }}
          >
            {p}
          </button>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 6: Wire `period` into `usePerformance`**

Edit `web/lib/usePerformance.ts`. Replace the whole file with:

```typescript
"use client";

import { useMemo } from "react";
import { useSyncHook, type UseSyncReturn } from "./useSyncHook";
import type { PerformanceData, PerformancePeriod } from "./types";

/** Hook is broker- AND period-aware: the endpoint URL embeds both, so each
 *  (broker, period) combination keys its own cache entry inside useSyncHook's
 *  module-level map. Switching either swaps cache entries (no stale results). */
export function usePerformance(
  active: boolean,
  broker: "IB" | "FUTU" = "IB",
  period: PerformancePeriod = "YTD",
): UseSyncReturn<PerformanceData> {
  const config = useMemo(
    () => ({
      endpoint: `/api/performance?broker=${encodeURIComponent(broker)}&period=${encodeURIComponent(period)}`,
      interval: 15 * 60 * 1000,
      hasPost: false,
      extractTimestamp: (data: PerformanceData) =>
        data.status === "ok"
          ? (data.last_sync ?? data.as_of ?? null)
          : (data.last_sync ?? null),
    }),
    [broker, period],
  );
  return useSyncHook<PerformanceData>(config, active);
}
```

- [ ] **Step 7: Forward `?period=` in the Next.js route proxy**

Edit `web/app/api/performance/route.ts`. Locate:

```typescript
const broker = request.nextUrl.searchParams.get("broker") ?? "IB";
const path = `/performance?broker=${encodeURIComponent(broker)}`;
```

Replace with:

```typescript
const broker = request.nextUrl.searchParams.get("broker") ?? "IB";
const period = request.nextUrl.searchParams.get("period") ?? "YTD";
const path = `/performance?broker=${encodeURIComponent(broker)}&period=${encodeURIComponent(period)}`;
```

- [ ] **Step 8: Run tests, confirm pass**

```bash
cd web && npm test -- performance-period-selector use-performance-period
```

Expected: 7 passed.

- [ ] **Step 9: Typecheck**

```bash
cd web && npm run typecheck
```

Expected: no errors. If the `PerformanceSummary` field name in `types.ts` is different from what `_fill_return_flavors` writes (it writes `simple_total_return`, `twr_total_return`, `irr_total_return`, `net_external_flows` — all snake_case), the typecheck will fail loudly. The names must match byte-for-byte.

- [ ] **Step 10: Commit**

```bash
git add web/components/PerformancePeriodSelector.tsx \
        web/lib/types.ts \
        web/lib/usePerformance.ts \
        web/app/api/performance/route.ts \
        web/tests/performance-period-selector.test.ts \
        web/tests/use-performance-period.test.ts
git commit -m "feat(perf-ui): period selector component + types

PerformancePeriodSelector — 4 pills (1M/3M/YTD/All), active via
aria-pressed. usePerformance accepts a period arg and embeds it
in the endpoint URL so useSyncHook caches per (broker, period).
Next.js proxy forwards ?period= to FastAPI. Summary type gains
the four PR-2 honest-% fields."
```

---

## Task 7: Frontend — headline tooltip + mount into `PerformancePanel`

**Background:** Replace the old single-value headline render with a Simple-as-headline + tooltip-with-three-more pattern. The tooltip is keyboard-accessible (hover OR focus on the info icon shows it; Escape dismisses) and rendered as a positioned `<div>` near the headline. Adopt the same Lucide icon pattern PerformancePanel already uses (`AlertTriangle`, `Gauge`, etc.) — `Info` icon from lucide-react.

`PerformancePanel` mounts the period selector above the chart and threads `period` state through React state.

**Files:**

- Create: `web/components/PerformanceHeadlineTooltip.tsx`
- Modify: `web/components/PerformancePanel.tsx`
- Test: `web/tests/performance-headline-tooltip.test.ts`

- [ ] **Step 1: Write the failing test**

Create `web/tests/performance-headline-tooltip.test.ts`:

```typescript
import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import PerformanceHeadlineTooltip from "@/components/PerformanceHeadlineTooltip";

const SUMMARY = {
  total_return: 0.123,
  simple_total_return: 0.123,
  twr_total_return: 0.118,
  irr_total_return: 0.116,
  net_external_flows: 2700,
};

describe("PerformanceHeadlineTooltip", () => {
  it("renders the headline (simple) value", () => {
    render(<PerformanceHeadlineTooltip summary={SUMMARY} currency="USD" />);
    expect(screen.getByTestId("performance-headline")).toHaveTextContent("+12.30%");
  });

  it("info icon is keyboard-focusable", () => {
    render(<PerformanceHeadlineTooltip summary={SUMMARY} currency="USD" />);
    const icon = screen.getByTestId("performance-headline-info");
    expect(icon).toHaveAttribute("tabindex", "0");
  });

  it("shows TWR, IRR, and net deposits on hover", async () => {
    render(<PerformanceHeadlineTooltip summary={SUMMARY} currency="USD" />);
    fireEvent.mouseEnter(screen.getByTestId("performance-headline-info"));
    const tooltip = await screen.findByTestId("performance-headline-tooltip");
    expect(tooltip).toHaveTextContent("Time-Weighted");
    expect(tooltip).toHaveTextContent("+11.80%");
    expect(tooltip).toHaveTextContent("Money-Weighted");
    expect(tooltip).toHaveTextContent("+11.60%");
    expect(tooltip).toHaveTextContent("Net deposits");
    expect(tooltip).toHaveTextContent("+$2,700");
  });

  it("hides tooltip on mouse leave", async () => {
    render(<PerformanceHeadlineTooltip summary={SUMMARY} currency="USD" />);
    const info = screen.getByTestId("performance-headline-info");
    fireEvent.mouseEnter(info);
    await screen.findByTestId("performance-headline-tooltip");
    fireEvent.mouseLeave(info);
    expect(screen.queryByTestId("performance-headline-tooltip")).toBeNull();
  });

  it("renders dashes for null TWR/IRR without crashing", () => {
    render(
      <PerformanceHeadlineTooltip
        summary={{
          total_return: 0.05,
          simple_total_return: 0.05,
          twr_total_return: null,
          irr_total_return: null,
          net_external_flows: 0,
        }}
        currency="USD"
      />,
    );
    fireEvent.mouseEnter(screen.getByTestId("performance-headline-info"));
    const tooltip = screen.getByTestId("performance-headline-tooltip");
    expect(tooltip.textContent).toContain("---");
  });
});
```

- [ ] **Step 2: Run test, confirm it fails**

```bash
cd web && npm test -- performance-headline-tooltip
```

Expected: fails with `Cannot find module '@/components/PerformanceHeadlineTooltip'`.

- [ ] **Step 3: Implement the tooltip**

Create `web/components/PerformanceHeadlineTooltip.tsx`:

```typescript
"use client";

import { Info } from "lucide-react";
import { useState } from "react";

type Summary = {
  total_return: number | null | undefined;
  simple_total_return: number | null | undefined;
  twr_total_return: number | null | undefined;
  irr_total_return: number | null | undefined;
  net_external_flows: number | null | undefined;
};

const DASH = "---";

function fmtPct(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`;
}

function fmtUsd(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  const sign = value >= 0 ? "+" : "-";
  const abs = Math.abs(value);
  return `${sign}$${abs.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

export default function PerformanceHeadlineTooltip({
  summary,
  currency,
}: {
  summary: Summary;
  currency: string;
}) {
  const [open, setOpen] = useState(false);
  // Headline uses simple if present, falls back to total_return.
  const headline = summary.simple_total_return ?? summary.total_return ?? null;
  return (
    <span className="performance-headline-wrapper">
      <span
        className={`performance-headline ${headline != null && headline >= 0 ? "positive" : headline != null ? "negative" : "neutral"}`}
        data-testid="performance-headline"
      >
        {fmtPct(headline)}
      </span>
      <span
        role="button"
        tabIndex={0}
        aria-label="Show return breakdown"
        data-testid="performance-headline-info"
        className="performance-headline-info"
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => {
          if (e.key === "Escape") setOpen(false);
        }}
      >
        <Info size={14} aria-hidden />
      </span>
      {open && (
        <div
          className="performance-headline-tooltip"
          role="tooltip"
          data-testid="performance-headline-tooltip"
        >
          <div className="tooltip-row">
            <span className="tooltip-label">Simple (flow-adj)</span>
            <span className="tooltip-value">{fmtPct(summary.simple_total_return)}</span>
          </div>
          <div className="tooltip-row">
            <span className="tooltip-label">Time-Weighted (TWR)</span>
            <span className="tooltip-value">{fmtPct(summary.twr_total_return)}</span>
          </div>
          <div className="tooltip-row">
            <span className="tooltip-label">Money-Weighted (IRR)</span>
            <span className="tooltip-value">{fmtPct(summary.irr_total_return)}</span>
          </div>
          <div className="tooltip-row tooltip-row-divider">
            <span className="tooltip-label">Net deposits ({currency})</span>
            <span className="tooltip-value">{fmtUsd(summary.net_external_flows)}</span>
          </div>
        </div>
      )}
    </span>
  );
}
```

- [ ] **Step 4: Run tooltip test, confirm pass**

```bash
cd web && npm test -- performance-headline-tooltip
```

Expected: 5 passed.

- [ ] **Step 5: Mount selector + tooltip in `PerformancePanel`**

Edit `web/components/PerformancePanel.tsx`. Make four changes:

(a) Add imports near the top:

```typescript
import PerformancePeriodSelector from "./PerformancePeriodSelector";
import PerformanceHeadlineTooltip from "./PerformanceHeadlineTooltip";
import type { PerformancePeriod } from "@/lib/types";
```

(b) Locate the call to `usePerformance(...)` inside the `PerformancePanel` component. It currently looks something like:

```typescript
const { data, ... } = usePerformance(active, broker);
```

Add a `useState` above it (just inside the component body, before any other hooks):

```typescript
const [period, setPeriod] = useState<PerformancePeriod>("YTD");
```

And update the hook call to pass period:

```typescript
const { data, ... } = usePerformance(active, broker, period);
```

(`useState` must already be imported — check the existing imports at the top of the file; if not, add it to the `react` import line.)

(c) Locate where the chart panel is rendered (the `<PerformanceChart>` call or the wrapping `<ChartPanel>`). Above it, mount the selector. Insert this just before the chart:

```typescript
<div className="performance-controls">
  <PerformancePeriodSelector value={period} onChange={setPeriod} />
</div>
```

(d) Replace the headline `total_return` render with the tooltip. Search the file for `total_return` — it's likely in a card definition like `{ id: "return", label: "Total Return", value: fmtPct(summary.total_return), ... }`. Replace the `value:` field with the tooltip component. The exact pattern depends on the current card shape — if the card config requires a string `value`, swap the whole `<StatCard ... />` for `<div className="metric-card-with-tooltip">...<PerformanceHeadlineTooltip summary={data.summary} currency={data.currency} />...</div>`.

> **Implementation tip:** if `PerformancePanel.tsx` builds cards from an array (the `PerformanceCardConfig` type at lines ~66-75 suggests it does), pull the headline card OUT of the array, render it separately as a dedicated `<div>` containing `<div class="metric-label">Total Return</div>` and `<PerformanceHeadlineTooltip ... />`, then render the remaining cards from the array as before.

(e) **Pass-3 A4 mitigation — data-freshness subtitle.** When close rows arrive at 17:30 ET, the chart and headline change retroactively for past dates. Surface this to the user with a small subtitle below the headline that names the source mix.

Create `web/components/PerformanceFreshness.tsx`:

```typescript
"use client";

import type { PerformanceData } from "@/lib/types";

/** Show the user where the displayed numbers come from. When close rows
 * arrive at 17:30 ET, the underlying NAV series shifts — this subtitle
 * makes that visible instead of letting the headline silently change. */
export default function PerformanceFreshness({ data }: { data: PerformanceData }) {
  if (data.status !== "ok") return null;
  const sources = new Set<string>();
  for (const row of data.series ?? []) {
    if (row.source) sources.add(row.source);
  }
  const sourceLabel =
    sources.size === 0 ? "no data" :
    sources.size === 1 ? Array.from(sources)[0] :
    "intraday + close";
  const lastSync = data.last_sync ?? data.as_of ?? "—";
  return (
    <div className="performance-freshness" data-testid="performance-freshness">
      Last refresh {lastSync} · Sources: {sourceLabel}
    </div>
  );
}
```

Mount the freshness subtitle immediately below the headline card in `PerformancePanel.tsx`:

```typescript
<div className="performance-headline-card">
  <div className="metric-label">Total Return</div>
  <PerformanceHeadlineTooltip summary={data.summary} currency={data.currency} />
  <PerformanceFreshness data={data} />
</div>
```

Type-extension to `web/lib/types.ts` (if `series` rows don't already include `source` in the existing type, add it):

```typescript
type PerformanceSeriesPoint = {
  date: string;
  nav: number;
  source?: "intraday" | "close"; // Pass-3 A4: present once Task 0 ships
  // ...other existing fields
};
```

Add the test to `web/tests/performance-headline-tooltip.test.ts`:

```typescript
describe("PerformanceFreshness", () => {
  it("renders 'intraday + close' when both sources present in series", () => {
    const data = {
      status: "ok" as const,
      currency: "USD",
      last_sync: "2026-06-03T17:35:00Z",
      summary: {} as any,
      series: [
        { date: "2026-06-01", nav: 100, source: "close" },
        { date: "2026-06-03", nav: 101, source: "intraday" },
      ],
    } as any;
    render(<PerformanceFreshness data={data} />);
    expect(screen.getByTestId("performance-freshness")).toHaveTextContent("intraday + close");
  });

  it("renders single source label when only one present", () => {
    const data = {
      status: "ok" as const,
      currency: "USD",
      last_sync: "2026-06-03T15:00:00Z",
      summary: {} as any,
      series: [{ date: "2026-06-03", nav: 100, source: "intraday" }],
    } as any;
    render(<PerformanceFreshness data={data} />);
    expect(screen.getByTestId("performance-freshness")).toHaveTextContent("intraday");
  });
});
```

This closes Pass-3 A4: the retroactive change is now visible to the user, not silent.

- [ ] **Step 6: Run full Vitest suite (catches collateral breakage in PerformancePanel tests)**

```bash
cd web && npm test
```

Expected: all pre-existing performance-related tests still pass (`performance-chart-theme`, `performance-chart-axes`, `performance-route`, `performance-freshness`, `performance-chart-model`). If any test asserts on the OLD card shape, update it to match — but only after confirming the visual diff (next task) is intentional.

- [ ] **Step 7: Typecheck**

```bash
cd web && npm run typecheck
```

Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add web/components/PerformanceHeadlineTooltip.tsx \
        web/components/PerformancePanel.tsx \
        web/tests/performance-headline-tooltip.test.ts
git commit -m "feat(perf-ui): headline tooltip + period selector mounted

PerformancePanel grows a period state (default YTD) threaded into
usePerformance. New PerformanceHeadlineTooltip swaps the plain
total_return % with a Simple-as-headline + hover/focus tooltip
that surfaces TWR, IRR, and net deposits."
```

---

## Task 8: E2E browser verification + PR

**Background:** Per the mandatory rules in root `CLAUDE.md` § ⛔ Mandatory Rules: "E2E browser verification for ALL UI work. Primary: `chrome-cdp`. Fallback: Playwright. No UI change done until visually confirmed." We add one Playwright spec that drives the period selector against a stub-backed dev server and one chrome-cdp run for visual confirmation, then open the PR.

**Tooling preflight (Pass-6 patch — closes the previous 7/10 disclosure on environment dependency):** Run this once at task start to decide which path applies:

```bash
# 1. Check chrome-cdp availability (Claude Code MCP integration)
# If the chrome-devtools-mcp plugin is installed and a Chrome instance is reachable,
# use chrome-cdp for Step 2. Skip Step 2 entirely if not — Playwright (Step 3+4)
# is the durable fallback and is already wired (web/playwright.config.ts exists).

# 2. Verify Playwright is installed
cd web && npx playwright --version || npx playwright install --with-deps chromium
```

Either path SATISFIES the mandatory rule (chrome-cdp is preferred for visual review; Playwright is sufficient for assertion-based confirmation). The PR description must note which check ran. Never declare the UI "done" with only the unit tests green — that's the failure mode the rule blocks.

**Files:**

- Create: `web/e2e/performance-period-selector.spec.ts`

- [ ] **Step 1: Start the dev stack against paper**

```bash
cd /Users/chenxi/projects/xenon
scripts/infra/dev.sh paper
```

Wait for:

- FastAPI on :8321 healthy
- Next.js on :3000 ready
- `dev.sh` printed "ready" or similar

In another shell, smoke-check the new route:

```bash
curl -sS 'http://localhost:8321/performance?period=1M&broker=IB' | jq '.period_start, .summary | {simple_total_return, twr_total_return, irr_total_return, net_external_flows}'
```

Expected: `period_start` is roughly 30 days back from today; summary contains the four new fields (IB → `twr_total_return` and `irr_total_return` may be `null`; that's fine).

- [ ] **Step 2: Visual check via chrome-cdp**

Open the running dev server in chrome-cdp:

- Navigate to `http://localhost:3000/performance`
- Take a screenshot — confirm the period selector (4 pills) is visible above the chart, default selection is `YTD`
- Click `3M` — chart should re-render with a narrower window, headline % should update, network shows `GET /api/performance?broker=IB&period=3M`
- Hover the `ⓘ` icon next to the headline — tooltip should display Simple, TWR, IRR, and Net deposits lines (TWR/IRR may show `---` for IB)
- Switch the broker tab to FUTU (if a FUTU account is configured in the dev env) — repeat. FUTU should show populated TWR/IRR.

> **If chrome-cdp is unavailable** (e.g. CI / headless run), skip this step and rely on the Playwright spec in Step 3 alone. Note in the PR description which check was used.

- [ ] **Step 3: Write the Playwright spec**

Create `web/e2e/performance-period-selector.spec.ts`:

```typescript
import { test, expect } from "@playwright/test";

test.describe("Performance period selector", () => {
  test("defaults to YTD and switches to 1M", async ({ page }) => {
    await page.goto("/performance");
    await page.waitForLoadState("networkidle");

    // Selector is mounted.
    const selector = page.getByTestId("performance-period-selector");
    await expect(selector).toBeVisible();

    // YTD active by default.
    await expect(page.getByTestId("performance-period-YTD")).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    // Click 1M, confirm pressed-state and that a fresh /api/performance fires.
    const apiCall = page.waitForRequest(
      (req) =>
        req.url().includes("/api/performance") &&
        req.url().includes("period=1M"),
    );
    await page.getByTestId("performance-period-1M").click();
    await apiCall;
    await expect(page.getByTestId("performance-period-1M")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(page.getByTestId("performance-period-YTD")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  test("headline tooltip reveals TWR / IRR / net deposits", async ({
    page,
  }) => {
    await page.goto("/performance");
    await page.waitForLoadState("networkidle");

    const info = page.getByTestId("performance-headline-info");
    await expect(info).toBeVisible();

    await info.hover();
    const tooltip = page.getByTestId("performance-headline-tooltip");
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toContainText("Simple (flow-adj)");
    await expect(tooltip).toContainText("Time-Weighted");
    await expect(tooltip).toContainText("Money-Weighted");
    await expect(tooltip).toContainText("Net deposits");
  });
});
```

- [ ] **Step 4: Run Playwright spec**

```bash
cd web && npx playwright test performance-period-selector --reporter=line
```

Expected: 2 passed (assuming the dev server from Step 1 is still running on :3000).

- [ ] **Step 5: Final regression sweep**

```bash
cd /Users/chenxi/projects/xenon
uv run python scripts/infra/dev/run_pytest_affected.py
cd web && npm test && npm run typecheck && npm run lint
```

Expected: all green.

- [ ] **Step 6: Commit the spec**

```bash
cd /Users/chenxi/projects/xenon
git add web/e2e/performance-period-selector.spec.ts
git commit -m "test(perf-ui): playwright e2e for period selector + tooltip"
```

- [ ] **Step 7: Push branch + open PR**

```bash
git push -u origin feat/performance-holistic-upgrade
gh pr create --title "feat(perf): period selector + FUTU honest-% + tooltip" \
  --body "$(cat <<'EOF'
## Summary
- \`GET /performance\` accepts \`?period=1M|3M|YTD|All\` (default YTD).
- Period switches re-compute series **and** summary metrics for the window; cache key includes period.
- FUTU returns now flow-adjusted via \`xenon.futu_cash_flow\`; FUTU risk metrics (Sharpe/Sortino/etc.) no longer masked.
- Summary gains four fields: \`simple_total_return\`, \`twr_total_return\`, \`irr_total_return\`, \`net_external_flows\`. Headline = simple; tooltip surfaces all four.
- IB stays on current dailyPnL returns + masked-by-default risk metrics — IB honest-% is a follow-up (needs Flex CashTransaction extraction).

Closes the three deferred items from \`docs/superpowers/specs/2026-05-31-performance-rebuild-design.md\` (period selector, honest total_return, FUTU TWR follow-up).

## Test plan
- \`uv run pytest scripts/tests/test_performance_periods.py scripts/tests/test_performance_returns.py scripts/tests/test_performance_futu_flows.py scripts/tests/test_performance_period_e2e.py scripts/tests/test_performance_futu_unmasked.py scripts/tests/test_nav_history_period_end.py -v\`
- \`cd web && npm test\`
- \`cd web && npx playwright test performance-period-selector\`
- Visual check: chrome-cdp on \`http://localhost:3000/performance\` — period selector + tooltip rendered correctly across IB and FUTU broker tabs.
EOF
)"
```

- [ ] **Step 8: Wait for CI, merge**

```bash
gh pr checks --json conclusion,name | jq '.[] | select(.conclusion != null)'
# If all green:
gh pr merge --squash --delete-branch
```

Plan complete (UI half).

---

## Task 9: Regression-pin `fetch_ib_nav_series` source='close' (Pass-2 T10 — reframed)

**Background (Pass-2 revised):** This change already shipped on the current branch — `src/xenon/reports/portfolio_performance.py:429` passes `source="close"` to `upsert_nav_sync`. Verified via grep on 2026-06-03 before Pass 3. The task is now a **regression-pin**: ensure the existing behavior is covered by a test so a future refactor cannot silently regress it.

**Files:**

- Modify: `src/xenon/reports/portfolio_performance.py` — locate the `upsert_nav_sync(...)` call inside `fetch_ib_nav_series` and add `source="close"`
- Test: `scripts/tests/test_fetch_ib_nav_series_source.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""fetch_ib_nav_series must persist source='close' (Pass-1)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine

_SAMPLE_FLEX_XML = """<?xml version="1.0"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement accountId="DUQ999999">
      <EquitySummaryInBase>
        <EquitySummaryByReportDateInBase reportDate="20260601"
          total="100" cash="50" stock="40" options="10"/>
      </EquitySummaryInBase>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


def test_fetch_ib_nav_series_writes_source_close(monkeypatch, pg_test_engine):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")

    def fake_urlopen(url, timeout=30):
        class _R:
            def __init__(self, body):
                self._body = body
            def read(self):
                return self._body.encode()
        if "SendRequest" in url:
            return _R(
                '<?xml version="1.0"?><FlexStatementResponse>'
                '<Status>Success</Status><ReferenceCode>REF123</ReferenceCode>'
                '</FlexStatementResponse>'
            )
        return _R(_SAMPLE_FLEX_XML)

    with patch("urllib.request.urlopen", fake_urlopen), patch("time.sleep"):
        from xenon.reports.portfolio_performance import fetch_ib_nav_series
        entries = fetch_ib_nav_series()

    assert entries is not None and len(entries) == 1

    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT source FROM xenon.nav_history "
                "WHERE broker='IB' AND account_env='paper' "
                "AND broker_account='DUQ999999' AND date='2026-06-01'"
            )
        ).first()
    assert row is not None
    assert row.source == "close"
```

- [ ] **Step 2: Run test, confirm fail.** Server default `'intraday'` instead of `'close'`.

- [ ] **Step 3: Implement.** Edit `src/xenon/reports/portfolio_performance.py` — locate the `upsert_nav_sync(...)` call inside `fetch_ib_nav_series` (currently around lines 390-398) and add `source="close"` as the final keyword arg.

- [ ] **Step 4: Run test, confirm pass.**

- [ ] **Step 5: Commit.**

```bash
git add src/xenon/reports/portfolio_performance.py scripts/tests/test_fetch_ib_nav_series_source.py
git commit -m "fix(nav): fetch_ib_nav_series writes source='close'

EquitySummaryByReportDateInBase rows are post-close — tag them so
they cannot be clobbered by an intraday writer's NAV the same day.

Latent bug surfaced when wiring Task 10's daily refresh CLI: every
prior Flex import wrote source='intraday' (server default)."
```

---

## Task 10: Patch existing `xenon-nav-flex-refresh` with READ_ONLY guard (Pass-2 T8 — reframed)

**Background (Pass-2 revised):** This CLI already exists on the current branch — `src/xenon/jobs/nav_flex_refresh.py` (2.6K, verified on 2026-06-03). It correctly derives `XENON_BROKER_ACCOUNT` and exits with codes 0/1/2. **Missing**: the `XENON_READ_ONLY=1` guard (Pass-1 callout — groovy missed it; the existing implementation also missed it). Add exit code 3 + the test.

**Files (Pass-2 revised):** Modify existing file rather than create new.

**Files:**

- Create: `src/xenon/jobs/__init__.py` (empty)
- Create: `src/xenon/jobs/nav_flex_refresh.py`
- Modify: `pyproject.toml` `[project.scripts]` — add `xenon-nav-flex-refresh`
- Test: `scripts/tests/test_nav_flex_refresh_cli.py` (create)

- [ ] **Step 1: Write the failing tests** (covers: no token → exit 2; no rows → exit 1; `XENON_READ_ONLY=1` → exit 3; broker-account env derivation from `XENON_LIVE_ACCOUNT` / `XENON_PAPER_ACCOUNT`; happy path → exit 0)

```python
"""xenon-nav-flex-refresh CLI behavior (Pass-1)."""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest


def _reimport_module():
    import xenon.jobs.nav_flex_refresh as m
    importlib.reload(m)
    return m


def test_main_exits_2_when_token_missing(monkeypatch, capsys):
    monkeypatch.delenv("IB_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("IB_FLEX_NAV_QUERY_ID", raising=False)
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    m = _reimport_module()
    assert m.main() == 2
    assert "FLEX_NOT_CONFIGURED" in capsys.readouterr().err


def test_main_exits_3_when_read_only(monkeypatch, capsys):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    m = _reimport_module()
    assert m.main() == 3
    assert "READ_ONLY" in capsys.readouterr().err


def test_main_exits_1_when_fetch_returns_none(monkeypatch):
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_LIVE_ACCOUNT", "U18007831")
    m = _reimport_module()
    with patch.object(m, "fetch_ib_nav_series", return_value=None):
        assert m.main() == 1


def test_main_derives_broker_account_from_live(monkeypatch, capsys):
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_LIVE_ACCOUNT", "U18007831")
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    m = _reimport_module()
    sample = [{"date": "2026-06-01", "total": 100.0, "cash": 50.0, "stock": 40.0, "options": 10.0}]
    with patch.object(m, "fetch_ib_nav_series", return_value=sample):
        assert m.main() == 0
    assert os.environ["XENON_BROKER_ACCOUNT"] == "U18007831"
    assert "fetched 1 NAV row" in capsys.readouterr().out


def test_main_derives_broker_account_from_paper(monkeypatch):
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ378889")
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    m = _reimport_module()
    with patch.object(m, "fetch_ib_nav_series", return_value=[{"date": "2026-06-01"}]):
        assert m.main() == 0
    assert os.environ["XENON_BROKER_ACCOUNT"] == "DUQ378889"
```

- [ ] **Step 2: Run tests, confirm fail** — `ModuleNotFoundError: No module named 'xenon.jobs'`.

- [ ] **Step 3: Create the package + module.**

```python
"""Daily IB Flex NAV refresh — invoked by launchd at 17:30 ET.

Polls IB Flex Web Service for EquitySummaryByReportDateInBase rows and
upserts them into xenon.nav_history with source='close'. The underlying
fetch_ib_nav_series handles the two-step SendRequest + GetStatement
polling and the upsert.

Exit codes:
  0 — fetched and persisted N>0 rows
  1 — fetch returned None or empty (token rejected, poll timeout, no rows)
  2 — FLEX_NOT_CONFIGURED (missing IB_FLEX_TOKEN or IB_FLEX_NAV_QUERY_ID)
  3 — READ_ONLY (XENON_READ_ONLY=1) — refusing to write
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    except ImportError:
        pass


def _ensure_broker_account_env() -> None:
    """Mirror scripts/infra/dev.sh:174 — derive XENON_BROKER_ACCOUNT from mode."""
    if os.environ.get("XENON_BROKER_ACCOUNT"):
        return
    mode = os.environ.get("XENON_TRADING_MODE", "").strip().lower()
    env_key = {"live": "XENON_LIVE_ACCOUNT", "paper": "XENON_PAPER_ACCOUNT"}.get(mode)
    if env_key and os.environ.get(env_key):
        os.environ["XENON_BROKER_ACCOUNT"] = os.environ[env_key]


from xenon.reports.portfolio_performance import fetch_ib_nav_series  # noqa: E402


def main() -> int:
    _load_env()

    # Pass-1 addition: refuse to write under XENON_READ_ONLY=1.
    if os.environ.get("XENON_READ_ONLY") == "1":
        print(
            "READ_ONLY: XENON_READ_ONLY=1 — refusing to ingest NAV rows. "
            "Unset the flag (or run on the macmini prod stack) to enable.",
            file=sys.stderr,
        )
        return 3

    _ensure_broker_account_env()

    if not os.environ.get("IB_FLEX_TOKEN") or not os.environ.get("IB_FLEX_NAV_QUERY_ID"):
        print("FLEX_NOT_CONFIGURED: set IB_FLEX_TOKEN and IB_FLEX_NAV_QUERY_ID", file=sys.stderr)
        return 2

    print(
        f"xenon-nav-flex-refresh: mode={os.environ.get('XENON_TRADING_MODE')} "
        f"account={os.environ.get('XENON_BROKER_ACCOUNT')}"
    )
    print("polling IB Flex Web Service (last-N-days query, ~30-90s)...")

    entries = fetch_ib_nav_series()
    if not entries:
        msg = "returned None — token rejected/poll timeout/no rows" if entries is None else "returned 0 rows"
        print(f"fetch_ib_nav_series {msg}", file=sys.stderr)
        return 1

    plural = "s" if len(entries) != 1 else ""
    print(f"fetched {len(entries)} NAV row{plural} (source='close' persisted via upsert_nav_sync)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Register entry point** in `pyproject.toml` `[project.scripts]`:

```toml
xenon-nav-flex-refresh   = "xenon.jobs.nav_flex_refresh:main"
```

- [ ] **Step 5: Re-sync & smoke-test import.**

```bash
uv sync --extra test
uv run python -c "from xenon.jobs.nav_flex_refresh import main; print('ok')"
```

- [ ] **Step 6: Run tests, confirm pass.** Expected: 5 passed.

- [ ] **Step 7: Commit.**

```bash
git add src/xenon/jobs/__init__.py src/xenon/jobs/nav_flex_refresh.py pyproject.toml scripts/tests/test_nav_flex_refresh_cli.py
git commit -m "feat(nav): xenon-nav-flex-refresh CLI

Daily entry point that polls IB Flex Web Service and upserts NAV rows
with source='close'. Exit codes: 0=ok, 1=fetch failed, 2=not
configured, 3=read-only. Wired up for the LaunchAgent in Task 11."
```

---

## Task 11: Patch existing LaunchAgent wrapper + plist (Pass-4 reframe)

**Background (Pass-4 revised):** Both files already exist on the current branch (verified 2026-06-03):

- `scripts/infra/nav-flex-refresh.sh` (2.1K) — currently has `export XENON_TRADING_MODE="${XENON_TRADING_MODE:-live}"` which Pass-2 T7 flagged as unsafe.
- `scripts/infra/launchd/com.xenon.nav-flex-refresh.plist` (2.4K) — structurally correct, pins `TZ=America/New_York`, `StartCalendarInterval Hour=17 Minute=30`, `RunAtLoad = false`. Modeled on `com.xenon.refresh-core-test.plist`.

This task is now: **patch the existing wrapper** with the Pass-2 T7 fail-fast (replace the silent `live` default with an explicit guard). The plist needs no change. Operator runbook (Task 12) gets a "macmini `.env` must set `XENON_TRADING_MODE=live`" line.

**Files (Pass-4 revised):** Modify existing files; no new creates.

- [ ] **Step 1: Write `nav-flex-refresh.sh`** — bash wrapper sourcing `.env`, running `uv run xenon-nav-flex-refresh`, logging to `/var/log/xenon/nav-flex-refresh.log`. See `scripts/infra/refresh-core-test.sh` for the structural template. Make executable with `chmod +x`. The content is mostly from the original groovy plan Task 4 Step 1, **with one Pass-2 T7 change**: replace the line

```bash
export XENON_TRADING_MODE="${XENON_TRADING_MODE:-live}"
```

with a fail-fast check:

```bash
# Pass-2 T7 — no silent default. The macmini runs both paper and live stacks;
# silently defaulting to live risks scope-collision when .env is misconfigured.
if [[ -z "${XENON_TRADING_MODE:-}" ]]; then
  log "FATAL: XENON_TRADING_MODE not set. Source .env first (and ensure it sets the mode), or set the var explicitly."
  exit 2
fi
case "$XENON_TRADING_MODE" in
  live|paper) ;;
  *)
    log "FATAL: invalid XENON_TRADING_MODE=$XENON_TRADING_MODE (must be 'live' or 'paper')."
    exit 2
    ;;
esac
```

The prod runbook (Task 12) sets `XENON_TRADING_MODE=live` explicitly in the macmini `.env`. Operator-local paper testing requires the same explicit set.

- [ ] **Step 2: Make executable + lint:** `chmod +x scripts/infra/nav-flex-refresh.sh && bash -n scripts/infra/nav-flex-refresh.sh`.

- [ ] **Step 3: Dry-run wrapper locally (paper mode).** `XENON_TRADING_MODE=paper XENON_NAV_REFRESH_LOG_DIR=/tmp/xenon-test ./scripts/infra/nav-flex-refresh.sh --dry`. Expected: exits 0 with "DRY: env sourced, mode=paper" line; log file at `/tmp/xenon-test/nav-flex-refresh.log` exists.

- [ ] **Step 4: Write the LaunchAgent plist.** Identical to the original groovy plan Task 4 Step 4 — copy-paste verbatim from `~/.claude/plans/groovy-purring-cerf.md` if convenient. Notably: `Label = com.xenon.nav-flex-refresh`, `StartCalendarInterval = {Hour: 17, Minute: 30}`, env vars `PATH`/`XENON_ROOT`/`TZ=America/New_York`, log paths under `/var/log/xenon/`, `RunAtLoad = false`.

- [ ] **Step 5: Validate plist:** `plutil -lint scripts/infra/launchd/com.xenon.nav-flex-refresh.plist`.

- [ ] **Step 6: Commit.**

```bash
git add scripts/infra/nav-flex-refresh.sh scripts/infra/launchd/com.xenon.nav-flex-refresh.plist
git commit -m "feat(infra): nav-flex-refresh LaunchAgent + wrapper

Daily 17:30 ET schedule on the macmini. Wrapper sources .env from the
checkout so plist stays secret-free (matches refresh-core-test pattern).
Install runbook in docs/runbooks/nav-flex-refresh.md."
```

---

## Task 12: Install + Operations Runbook (Pass-6 patch — concrete spec)

**Files:** Create `docs/runbooks/nav-flex-refresh.md`.

- [ ] **Step 1: Write the runbook.** Required sections, with the Pass-2 + Pass-3 callouts that didn't exist in groovy:

  **`# nav-flex-refresh — Daily IB NAV Auto-Refresh`**

  ## One-time install (macmini)

  Prerequisites verbatim from groovy + add:
  - **Pass-2 T7**: macmini `.env` MUST set `XENON_TRADING_MODE=live` explicitly. The wrapper fails fast if unset.
  - **Pass-3 A2**: ensure `alembic upgrade head` has applied `2026_06_03_nav_history_source_in_pk` BEFORE the LaunchAgent fires for the first time. Verify with:
    ```bash
    psql -h 100.66.147.98 -U xenon_prod core_dev -c "
      SELECT conname FROM pg_constraint
      WHERE conrelid = 'xenon.nav_history'::regclass AND contype = 'p';"
    # Expect: nav_history_pkey
    # Verify columns include source:
    psql -h 100.66.147.98 -U xenon_prod core_dev -c "
      SELECT a.attname FROM pg_constraint c
      JOIN pg_attribute a ON a.attnum = ANY(c.conkey) AND a.attrelid = c.conrelid
      WHERE c.conrelid = 'xenon.nav_history'::regclass AND c.contype = 'p'
      ORDER BY array_position(c.conkey, a.attnum);"
    # Expect: broker, account_env, broker_account, date, source
    ```

  Install steps (from groovy verbatim — placeholder sub, plutil -lint, `launchctl bootstrap`).

  ## Smoke test
  - `launchctl kickstart -k gui/$(id -u)/com.xenon.nav-flex-refresh` then `tail -f /var/log/xenon/nav-flex-refresh.log`.

  ## Diagnostics

  | Symptom                                                                             | Check                                                                                                         |
  | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
  | Job never fires                                                                     | `launchctl list \| grep nav-flex`                                                                             |
  | `FATAL: XENON_TRADING_MODE not set`                                                 | Pass-2 T7 — `.env` missing the explicit mode                                                                  |
  | `FLEX_NOT_CONFIGURED`                                                               | `.env` missing token or query id                                                                              |
  | `READ_ONLY: XENON_READ_ONLY=1 — refusing`                                           | someone exported `XENON_READ_ONLY=1` in the launchd context — should not happen on prod                       |
  | `fetch returned None`                                                               | IB Flex throttle (1018) — wait 30+ min                                                                        |
  | `there is no unique or exclusion constraint matching the ON CONFLICT specification` | **Pass-3 A1** — schema migration didn't run before code deployed. Apply `alembic upgrade head`, then re-fire  |
  | Rows tagged `intraday`                                                              | regression in Task 9's source='close' (use `xenon-nav-reconcile` to detect)                                   |
  | Both intraday + close present, NAVs diverge                                         | run `xenon-nav-reconcile --since YYYY-MM-DD --until YYYY-MM-DD` (Task 13) — non-zero exit means discrepancies |

  ## Maintenance
  - **Pass-1 callout**: tail `/var/log/xenon/nav-flex-refresh.log` daily for the first week after install.
  - Flex tokens expire after 1 year — refresh `IB_FLEX_TOKEN` in `.env`; no LaunchAgent reload needed.
  - **Pass-3 A5**: after applying any `nav_history` schema migration, clear the perf_cache by restarting `xenon-api` (next cache TTL will rebuild with the new schema).

  ## Uninstall

  `launchctl bootout gui/$(id -u)/com.xenon.nav-flex-refresh && rm ~/Library/LaunchAgents/com.xenon.nav-flex-refresh.plist`

  ## Scope
  - **IB-only.** FUTU reconciliation is a future plan once a Futu daily-statement PDF parser is built (Pass-1 operator-confirmed deferral). FUTU cash-flow audit today is via `xenon.futu_cash_flow` row-level ingestion (PR #120).

This spec is concrete enough that an engineer can write the runbook without further design work.

- [ ] **Step 2: Commit.**

```bash
git add docs/runbooks/nav-flex-refresh.md
git commit -m "docs(runbook): nav-flex-refresh install + ops procedure"
```

---

## Task 13: `xenon-nav-reconcile` CLI (Pass-1 addition — net new)

**Background (Pass-2 revised):** Pass-1 finding C3 — neither plan shipped a surface for the operator to _see_ the audit data. This task closes that gap. Plain-SQL discrepancy report: for a scope and date range, find dates where `source='intraday'` row's NAV differs from the `source='close'` row's NAV by more than a tolerance. No new table, no extra schema migration (Task 0 ships the option-(a) PK change).

**Pass-2 E1 resolution (unanimous):** option (a) — intraday + close rows coexist for the same date under the new PK `(broker, account_env, broker_account, date, source)`. The reconcile CLI is now the canonical reader: per-date, both rows are available, comparison is direct.

SQL shape:

```sql
SELECT date,
       MAX(nav) FILTER (WHERE source='intraday') AS intra_nav,
       MAX(nav) FILTER (WHERE source='close')    AS close_nav
FROM xenon.nav_history
WHERE broker = :broker AND account_env = :env AND broker_account = :acct
  AND date BETWEEN :since AND :until
GROUP BY date
HAVING MAX(nav) FILTER (WHERE source='intraday') IS NOT NULL
   AND MAX(nav) FILTER (WHERE source='close')    IS NOT NULL
   AND abs(MAX(nav) FILTER (WHERE source='close')
         - MAX(nav) FILTER (WHERE source='intraday'))
     / NULLIF(MAX(nav) FILTER (WHERE source='intraday'), 0) > :tolerance_ratio
ORDER BY date ASC;
```

Each output row is a flagged date with both NAVs and the diff in bps. Dates with only one source row (e.g., today's intraday before 17:30 ET Flex fire, or a backfilled close row pre-dating the intraday writer) are silently excluded — they're not reconcilable, by definition.

**Files:**

- Modify: `pyproject.toml` `[project.scripts]` — add `xenon-nav-reconcile`
- Create: `src/xenon/jobs/nav_reconcile.py`
- Test: `scripts/tests/test_nav_reconcile_cli.py` (create)

- [ ] **Step 1: Write the failing tests** (covers: no rows → exit 0 with "no data" message; intraday ↔ close diff within tolerance → exit 0; diff exceeds tolerance → exit 4 + flagged date in output; date-range filter respected; `--scope` flag derives `AccountScope` correctly)

```python
"""xenon-nav-reconcile CLI behavior (Pass-1)."""

from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

SCOPE = AccountScope(broker="IB", account_env="live", broker_account="U18007831")


def _reimport():
    import xenon.jobs.nav_reconcile as m
    importlib.reload(m)
    return m


def test_main_no_rows_exits_0(monkeypatch, pg_test_engine, capsys):
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_LIVE_ACCOUNT", "U18007831")
    m = _reimport()
    assert m.main(["--since", "2026-01-01", "--until", "2026-06-01"]) == 0
    assert "no rows" in capsys.readouterr().out.lower()


def test_main_within_tolerance_exits_0(monkeypatch, pg_test_engine, capsys):
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_LIVE_ACCOUNT", "U18007831")
    # Intraday writes a row, then close overwrites — current schema only retains close.
    upsert_nav_sync(scope=SCOPE, day=date(2026, 1, 15), nav=Decimal("100000.00"), source="intraday")
    upsert_nav_sync(scope=SCOPE, day=date(2026, 1, 15), nav=Decimal("100000.05"), source="close")
    m = _reimport()
    # Tolerance ratio default 0.1% — diff of 5/100000 = 0.005% is within.
    assert m.main(["--since", "2026-01-01", "--until", "2026-02-01"]) == 0


def test_main_exceeds_tolerance_exits_4(monkeypatch, pg_test_engine, capsys):
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_LIVE_ACCOUNT", "U18007831")
    upsert_nav_sync(scope=SCOPE, day=date(2026, 2, 15), nav=Decimal("100000.00"), source="intraday")
    upsert_nav_sync(scope=SCOPE, day=date(2026, 2, 15), nav=Decimal("105000.00"), source="close")  # 5% off
    m = _reimport()
    assert m.main(["--since", "2026-01-01", "--until", "2026-03-01"]) == 4
    out = capsys.readouterr().out
    assert "2026-02-15" in out
```

> **Pass-2 T2 note:** under the new PK `(broker, account_env, broker_account, date, source)` from Task 0, the two `upsert_nav_sync` calls in each test produce TWO COEXISTING rows — exactly what we want. The previous test version was misleading because the old PK collapsed them; Pass-2's E1-(a) resolution makes the test honest. Verify by `SELECT COUNT(*) FROM xenon.nav_history WHERE date=:d` returns 2 after seeding.

- [ ] **Step 2: Run tests, confirm fail** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/xenon/jobs/nav_reconcile.py`** — argparse with `--since`, `--until`, `--tolerance-bps` (default 10 = 0.1%), `--broker`, `--account-env`, `--broker-account` (defaults from env via `_ensure_broker_account_env`). Queries `xenon.nav_history` scoped, runs the per-date intraday-vs-close SQL from the Background section, flags rows above tolerance. Print as a table (date | intra_nav | close_nav | diff_bps). Read-only CLI — `XENON_READ_ONLY=1` is logged for uniformity but does not change behavior. Exit code 4 when discrepancies found so cron / CI can alert.

- [ ] **Step 4: Register entry point** in `pyproject.toml`:

```toml
xenon-nav-reconcile      = "xenon.jobs.nav_reconcile:main"
```

- [ ] **Step 5: Run tests, confirm pass.** Expected: 3 passed.

- [ ] **Step 6: Commit.**

```bash
git add src/xenon/jobs/nav_reconcile.py pyproject.toml scripts/tests/test_nav_reconcile_cli.py
git commit -m "feat(nav): xenon-nav-reconcile CLI

Plain-SQL discrepancy report: intraday vs close NAV per date for a
scope, flagging rows where the diff exceeds the tolerance (default
10 bps). No new table — uses xenon.nav_history.source. Exit code 4
when discrepancies found so cron / CI can alert."
```

---

Plan complete.

---

## Future work (separate plans)

- **Futu daily-statement PDF parser.** Operator-confirmed deferral (Pass 1, 2026-06-03). Futu OpenD has no programmatic post-close NAV endpoint; the daily statement that ships from the Futu app is a PDF. Future plan: parse the PDF on a daily LaunchAgent, persist close rows with `source='close'`, hook into `xenon-nav-reconcile` so FUTU gets the same audit surface as IB. Until then, FUTU cash-flow audit is covered by `xenon.futu_cash_flow` row-by-row ingestion (PR #120).
- **IB honest-%** — extend the saved IB Flex query to include `CashTransaction`, persist to either a new `cash_flows` table or a new column on `nav_history`, then wire `_ib_cash_flows()` analogous to `load_futu_flows_per_day`. After that, the FUTU branch and IB branch can share the same `_fill_return_flavors` path and IB's `twr_total_return` / `irr_total_return` start populating.
- **Cross-broker `cash_flows` table** — once IB needs cash flows, it's cheap to consolidate into one canonical table that both brokers fill. Schema decision deferred until IB lands; YAGNI for now.
- **Half-day market sessions in cache TTL** — `perf_cache._ttl_for_now` doesn't know about 13:00 ET closes (Black Friday etc.). Open issue per Correction #26.
- **Period selector keyboard navigation** — `←` / `→` arrow keys between pills, `Enter` activates. Accessibility nice-to-have, not blocking.
