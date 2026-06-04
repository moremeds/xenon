# Performance Holistic Plan — Pass 2 Tribunal Review

**Date:** 2026-06-03
**Reviewers:** Codex (gpt-5.3-codex, weight 1.0) + Gemini (weight 0.5) + Claude (weight 1.0)
**Target:** `docs/superpowers/plans/2026-06-03-performance-holistic-upgrade.md` (post Pass-1 amendments)
**Verdict:** **FIX-FIRST** — 3-way agreement
**E1 vote:** **UNANIMOUS (a) drop PK + modify `nav_history_one_env_per_day`** (weight 2.5)

---

## Consensus findings (11 items, all applied)

| ID      | Severity | Finding                                                                                                                                                                                                                                                                   | Reviewers                              | Resolution                                                                                                                                                       |
| ------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **T1**  | CRITICAL | E1 audit story requires migrating BOTH the PK AND the secondary unique index `nav_history_one_env_per_day` (env-excluded, covers `(broker, broker_account, date)`). Dropping only the PK is insufficient — the secondary index still blocks same-date source coexistence. | Codex #1 + Gemini #5 + Claude C2       | Task 0 includes migration: new PK = `(broker, account_env, broker_account, date, source)`. Secondary index becomes a one-env-per-day-per-source guard.           |
| **T2**  | HIGH     | Task 13 reconciliation tests prove the wrong thing — under current PK the second `upsert_nav_sync` UPDATEs the first row, so "two rows for same date" never materializes.                                                                                                 | Codex #2 + Gemini #3 + Claude C4       | Post option-(a) migration: tests seed two same-date `source` rows; CLI compares per-date `intraday` vs `close` directly.                                         |
| **T3**  | HIGH     | `enforce_account_env_guard` opt-in is unsafe — developers forget to set it, the cross-env collision bug re-emerges.                                                                                                                                                       | Codex #5 + Gemini #4 (Claude conceded) | Invert: guard ON by default. Legacy unscoped callers use `_upsert_nav_sync_unguarded()`.                                                                         |
| **T4**  | HIGH     | Writer unification misses a **fourth writer**: `xenon.db.queries.portfolio.upsert_nav()` at `portfolio.py:188` does its own `pg_insert(nav_history)`, no `source` param.                                                                                                  | Codex #3 + Claude verify               | Task 0 migrates it too. CI guard: only `portfolio_loader.py` may `pg_insert(nav_history)`.                                                                       |
| **T5**  | HIGH     | FUTU race-safe IntegrityError catch is NOT actually preserved by Task 0 as written. The pre-INSERT SELECT guard does not handle the inter-process race where two writers both pass the SELECT and one hits the unique index.                                              | Codex #4 + Claude verify               | Hoist FULL race-safe pattern (SELECT + IntegrityError catch + rollback + re-query + raise `NavAccountEnvConflict`). Add explicit test forcing the DB-level race. |
| **T6**  | MEDIUM   | Sync `upsert_nav_sync()` inside async `persist_futu_nav` blocks the FastAPI event loop.                                                                                                                                                                                   | Codex #6 + Gemini #1 + Claude C3       | Provide BOTH sync (`upsert_nav_sync`) and async (`upsert_nav_async`) wrappers around shared SQL/upsert core. FUTU calls async.                                   |
| **T7**  | MEDIUM   | LaunchAgent wrapper defaults `XENON_TRADING_MODE=live` when `.env` omits it. Macmini runs both stacks — silent scope-collision risk.                                                                                                                                      | Codex #8 + Gemini #2 (Claude conceded) | Wrapper fails fast when mode unset. Prod runbook sets explicitly.                                                                                                |
| **T8**  | MEDIUM   | CLI already exists on current branch (`src/xenon/jobs/nav_flex_refresh.py`, 2.6K). Task 10 plans to create it. Existing code has no `XENON_READ_ONLY=1` check.                                                                                                            | Codex #7 only (Claude verified)        | Reframe Task 10: patch existing CLI to add READ_ONLY guard (exit 3) + add missing test.                                                                          |
| **T9**  | MEDIUM   | Task 5 period test expects `period_start == "2026-01-02"` but Task 1's resolver returns `"2026-01-01"` for YTD when inception is 2025-12-01 (Jan 1 is after inception, no clamp needed).                                                                                  | Codex #9 only (Claude verified)        | Fix test expectation to `2026-01-01`.                                                                                                                            |
| **T10** | LOW      | Task 9 already shipped on current branch — `fetch_ib_nav_series` already passes `source="close"` at `portfolio_performance.py:429`. The "failing test" won't fail.                                                                                                        | Codex #10 only (Claude verified)       | Reframe as regression-pin: verify the source='close' is preserved + add the test if missing.                                                                     |
| **T11** | CRITICAL | `pg_test_async_engine` fixture does NOT exist in `conftest.py:127,151`. Only `pg_test_engine` (sync) and `async_engine` (async) exist. Patched-plan Tasks 0, 4-extended, 5, 13 reference the missing name.                                                                | Claude C1 only                         | Globally rename `pg_test_async_engine` → `async_engine` in all plan test code blocks.                                                                            |

## Dismissed

- **Pass-1 SQL snippet docs error** (Gemini #6, conf 100, severity LOW) — Trivial — Pass-1 review's shorthand vs the plan's correct CASE-based SQL. No code action needed; plan implementation is correct.

## Debate

No formal debate round required. All initially-contested items reached consensus via concrete code evidence: Codex provided file:line references for T4, T5, T8, T9, T10; Claude verified independently with `grep`/`Read`. On T3 and T7, Claude conceded after seeing the stronger counter-argument from Codex+Gemini.

## E1 vote — Unanimous (a)

**Reasoning (all three reviewers converged):** Operator's "auditable without a new table" constraint is satisfied only when both `intraday` and `close` rows coexist as separate audit rows in `nav_history`. Option (b) loses audit data: either close overwrites intraday or intraday becomes a no-op when close exists. Option (a) has bounded migration cost (one migration + every nav_history reader updates), already prepared for by Pass-1's `DISTINCT ON` SQL in `load_nav_curve`.

**Critical addendum (Codex):** dropping only the PK is insufficient. `nav_history_one_env_per_day` at `schema.py:200` also blocks same-date source coexistence. New PK = `(broker, account_env, broker_account, date, source)`. Secondary index becomes a one-env-per-day-per-source guard.

## Standing-rule sweep

- ✅ No new Yahoo usage. Existing Yahoo fallbacks (`web/app/api/ticker/*`, `previous-close`, `info`) are pre-existing and out of scope.
- ✅ No naked-short paths touched.
- ✅ No new secrets handling.
- ✅ DB-first preserved.
- ✅ `uv`-only throughout.
- ✅ Broker scope discipline preserved across writer migrations (all writers will pass `AccountScope`).

## Review stats

- Total issues raised: 18 (Codex: 10, Gemini: 6, Claude: 8 with overlaps)
- Unanimous (3-way): 3 (T1, T2, T6)
- Strong consensus (Codex+Gemini, weight 1.5): 2 (T3, T7)
- Codex+Claude post-hoc verify: 2 (T4, T5)
- Codex-solo verified by Claude: 3 (T8, T9, T10)
- Claude-solo verified by Claude: 1 (T11)
- Dismissed: 1
- E1: unanimous (a) with secondary-index addendum

## Applied to plan

Pass-2 fixes T1–T11 applied to `docs/superpowers/plans/2026-06-03-performance-holistic-upgrade.md` via 8 surgical edits. See plan's Pass-2 amendments preface for the change log.

## Deferred to Pass 3 (adversarial)

- Concurrent LaunchAgent firings (manual `kickstart -k` + scheduled fire).
- Partial migration state: Task 0 ships writers unified but the schema migration is mid-flight when Task 10's CLI fires.
- Mid-period source flip changing headline retroactively.
- Network split during Flex poll leaving a half-written row.
- `_prev_nav` query in FUTU writer running against the test's snapshot view vs the shared writer's actual transaction.
