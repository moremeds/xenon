# Performance Holistic Plan — Pass 1 Self-Review

**Date:** 2026-06-03
**Reviewer:** Claude (self-review, /review-cycle Pass 1)
**Target plans reviewed (combined):**

- `docs/superpowers/plans/2026-06-03-performance-holistic-upgrade.md` (saved this session)
- Plan-mode draft `~/.claude/plans/groovy-purring-cerf.md` (NAV auto-refresh)

**Expanded spec (operator critique applied as Pass-1 spec additions):**

1. No code duplication; max reuse across brokers.
2. DB data auditable via the existing `xenon.nav_history.source` column. No new audit table.
3. Low-lag daily NAV save preserved (intraday writes must not get slower).
4. IB Flex + (deferred) Futu daily statement act as post-close reconciliation sources.

**Verdict:** FIX-FIRST. Plans miss three critical items that change the architecture; merging the two plans into one PR is the right unit of work.

---

## Verified facts (Pass-1 evidence)

| #   | Fact                                                                                                                                                                                                                                                             | Evidence                                                                         |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| F1  | `upsert_nav_sync` already accepts `source: str \| None = None` with null-safe semantics. Groovy Task 1 is shipped.                                                                                                                                               | `src/xenon/utils/portfolio_loader.py:125-184`                                    |
| F2  | IB intraday writer `_append_nav_snapshot` does its own `pg_insert(nav_history)` (60 lines), does NOT pass `source`, server default writes `'intraday'`.                                                                                                          | `src/xenon/execution/ib_sync.py:1032-1094`                                       |
| F3  | FUTU intraday writer `persist_futu_nav` does its own `pg_insert(nav_history)`, stamps `source='intraday'` explicitly, has race-safe IntegrityError-catch (lines 155-167) that must survive any migration.                                                        | `src/xenon/api/services/futu_nav_persistence.py:88-167`                          |
| F4  | `nav_history.source` has CHECK `source IN ('close', 'intraday')`, NOT NULL DEFAULT 'intraday'.                                                                                                                                                                   | Migration `260fabba18d6`, `db/schema.py:190,196`                                 |
| F5  | Futu OpenD SDK has `accinfo_query()` (live snapshot, line 280) and cash-flow history (line 1006, already ingested to `xenon.futu_cash_flow`). No post-close NAV ledger.                                                                                          | `.venv/lib/python3.13/site-packages/futu/trade/open_trade_context.py`            |
| F6  | Three parallel worktrees on overlapping nav-history work: `.worktrees/nav-flex-auto-refresh/`, `.worktrees/futu-perf/`, `.worktrees/futu-nav-backfill/`. Loud signal that a coordinating plan is overdue.                                                        | `ls .worktrees/`                                                                 |
| F7  | `XENON_READ_ONLY=1` is honored by both intraday writers but the groovy `xenon-nav-flex-refresh` CLI does not check it. Risk: a MacBook `dev.sh live` session that fires the LaunchAgent writes `core_test` close rows while the live IB connection is read-only. | `ib_sync.py:1039`, `futu_nav_persistence.py` (no flag check), groovy plan Task 3 |

---

## Findings

### Critical (High-confidence — apply this pass)

#### C1 — Groovy Task 1 is already merged

The `upsert_nav_sync(source=...)` work is on this branch. Groovy plan still describes it as a Task-1 add. Action: delete Task 1 from the merged plan; start at groovy's Task 2 (`fetch_ib_nav_series` writes `source='close'`).

#### C2 — Triple nav_history writer surface

Three independent writers (`upsert_nav_sync`, `_append_nav_snapshot`, `persist_futu_nav`) all `pg_insert(nav_history)` with their own version of the cross-env collision guard. The operator's "no duplication" critique lands here. Action: add **Task 0 — Unify nav_history writers**. Migrate writers #2 and #3 to delegate to `upsert_nav_sync`. Surface the race-safe IntegrityError-catch and cross-env guard inside the shared helper so neither caller loses the protection. Existing call sites convert their env-derived scope into an `AccountScope` at the boundary.

#### C3 — Audit story sits in PG with no surface

No CLI / route / job compares same-date `intraday` vs `close` rows. The audit data exists but nobody sees it. Action: add **Task 9 — `xenon-nav-reconcile` CLI**. Plain-SQL: `SELECT date, MAX(nav) FILTER (WHERE source='close') AS close_nav, MAX(nav) FILTER (WHERE source='intraday') AS intra_nav, ... FROM xenon.nav_history WHERE [scope] AND date BETWEEN ... GROUP BY date HAVING abs(close - intra) > tol`. No new table.

#### C4 — `load_nav_curve` prefer-close MUST ship in this plan, not "v2 follow-up"

The holistic plan currently treats every row as authoritative because today only intraday exists. The moment groovy's close writes land, the curve returns two rows per Flex-touched date and the chart kinks. Shipping these in separate PRs creates a guaranteed regression window. Action: extend Task 4 with prefer-close logic — `DISTINCT ON (broker, account_env, broker_account, date) ... ORDER BY date ASC, source='close' DESC, source='intraday' DESC` (close wins ties; falls through to intraday when only one row exists).

### Major (High-confidence — apply this pass)

#### E2 — `XENON_READ_ONLY=1` not honored by new scheduled CLIs

Standard pattern (early-exit with the ⏭ log line) used elsewhere; groovy didn't add it. Action: add the flag check at the top of `xenon-nav-flex-refresh` (groovy Task 3) and at the top of `xenon-nav-reconcile` (new C3 task — though reconcile is read-only and could just print, the operator deserves the same uniform surface).

#### T1, T2, T3 — Test coverage holes

- T1: No test that the IB intraday writer delegates to the unified surface with `source='intraday'`. After Task 0 ships, essential.
- T2: No prefer-close test for `load_nav_curve` (two-row same-date scenario).
- T3: Reconciliation CLI is net-new; needs its own tests.

Action: add these to the corresponding tasks.

### Major (Judgment-call — DEFERRED to Pass 2)

#### E1 — Concurrent same-date intraday + close writes flicker

IB activity poller fires at 17:30:05 ET with a late-tick NAV after the 17:30:00 ET Flex close write. Both go through the unique PK `(broker, account_env, broker_account, date)`. Whoever writes LAST wins on the `nav` column; `source` flickers.

Two architectural choices, both invasive:

- **(a)** Drop the unique PK; let multiple rows coexist with `source` joining the PK. Most auditable (both `close` and `intraday` rows visible for the same date), but requires schema migration + every reader to handle multiple rows.
- **(b)** Make intraday writes a no-op when a `close` row already exists for the date. Simpler, no schema change. Reader sees a consistent close row post-17:30 ET on Flex-touched dates.

The operator's "auditable" constraint slightly favors (a), but (b) ships in 1 task vs ~6. **Defer to Pass 2 — let `/codex-review` weigh in.**

### Major (Judgment-call — RESOLVED by operator)

#### C5 — Futu reconciliation source

Operator answer: IB-only reconciliation now; FUTU PDF parser deferred to a separate plan. Rationale: Futu cash-flow ingestion already provides row-by-row audit (PR #120 backbone). Synthesizing a FUTU close NAV would reconcile against itself (theater). PDF parser is a large fragile build that doesn't belong in this PR.

### Medium / Low

| #   | Finding                                                              | Action                              |
| --- | -------------------------------------------------------------------- | ----------------------------------- |
| E3  | Futu statement T+1 lag (when PDF parser eventually ships)            | Runbook note (future plan)          |
| X2  | Cache key tuple expands 3→4; FastAPI hot-reload leaves stale entries | One-line comment in `perf_cache.py` |
| T4  | No e2e for nightly LaunchAgent firing — operator-tested only         | Runbook: tail log for first week    |

---

## Standing-rule check

- ✅ No Yahoo Finance.
- ✅ No naked-short paths touched.
- ✅ No new secrets handling.
- ✅ DB-first preserved — no JSON read paths added.
- ✅ `uv` used throughout — no bare `python`/`pip`.
- ⚠ Writers #2 and #3 read scope from env today. Migrating them to `upsert_nav_sync` requires explicit `AccountScope` construction at the call site. Not a violation; a coupling that demands care in Task 0.

---

## Applied fixes (this pass)

The holistic plan (`docs/superpowers/plans/2026-06-03-performance-holistic-upgrade.md`) was edited inline with:

1. **Pass-1 amendments preface** at the top of the document noting supersession of groovy plan and the merged scope.
2. **Goal / Architecture / Scope** updated to reflect unified ingestion + UI.
3. **Task 0 — Unify nav_history writers** inserted before existing Task 1.
4. **Task 4 — prefer-close** added to `load_nav_curve` (failing test + DISTINCT ON impl + regression coverage).
5. **Tasks 9–13** appended covering: `fetch_ib_nav_series` source='close' fix, `xenon-nav-flex-refresh` CLI, LaunchAgent + plist, runbook, `xenon-nav-reconcile` CLI.
6. **Future work** updated: Futu PDF parser added as a separately-tracked follow-up; IB honest-% remains.

The plan-mode groovy plan (`~/.claude/plans/groovy-purring-cerf.md`) is superseded — operators should use the holistic plan as the single source of truth.

---

## Deferred to Pass 2 (`/codex-review`)

- **E1 schema decision:** drop unique PK on `nav_history` (option a) vs intraday no-op when close exists (option b). Codex panel votes; merged plan chooses.

## Deferred to Pass 3 (adversarial)

- Concurrent reconciliation jobs firing on the same scope (LaunchAgent + manual `kickstart -k`).
- Partial migration state: Task 0 ships writers unified, Task 9 ships close rows — what happens if Task 9 fires before Task 0's IB writer is migrated?
- Source flip mid-period changing the headline % retroactively.
- Network split during Flex poll leaving a half-written row.

## Deferred to Pass 5 (assumption verification)

- LaunchAgent on macmini fires under correct TZ when system clock is in UTC.
- `pg_test_async_engine` fixture exists (Task 3 test assumes it).
- `scripts/infra/refresh-core-test.sh` pattern is the right template for the new wrapper.
