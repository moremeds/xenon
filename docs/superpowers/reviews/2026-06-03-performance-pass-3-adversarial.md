# Performance Holistic Plan — Pass 3 Adversarial Review

**Date:** 2026-06-03
**Reviewer:** Claude (adversarial mindset — "how would I break this in production?")
**Target:** `docs/superpowers/plans/2026-06-03-performance-holistic-upgrade.md` (post-Pass-2 amendments)

---

## Method

Probe for: concurrent writers racing, partial migration state, retries on non-idempotent ops, scheduler race conditions, network split mid-operation, secret leakage in subprocess envs, rollback hazards, UX surprises from retroactive changes.

## Critical findings (3 applied)

### A1 (CRITICAL) — `on_conflict_do_update` index_elements not updated for new PK

**Where:** Task 0 Step 3b — `_build_upsert_stmt` helper, also the original `upsert_nav_sync` body.

**Attack:** Task 0's migration changes the PK from `(broker, account_env, broker_account, date)` to `(broker, account_env, broker_account, date, source)`. The `pg_insert(...).on_conflict_do_update(index_elements=[...])` clause in `_build_upsert_stmt` needs to name a unique constraint that actually exists. Reusing the old 4-column form means PG raises `there is no unique or exclusion constraint matching the ON CONFLICT specification` on every UPSERT post-migration.

**Verdict:** Real failure mode. Pass-2 introduced `_build_upsert_stmt` as a helper without spelling out the index_elements change. Any first-day user of the migrated writer would hit it immediately.

**Fix applied:** Task 0 Step 3b now explicitly shows the 5-column `index_elements` and includes the helper body verbatim.

### A2 (HIGH) — Macmini deploy ordering hazard

**Where:** Task 0 Step 3a — migration application.

**Attack:** The macmini Docker stack runs `migrator` and `api` services. If `api` boots before `migrator` completes the new migration, new code with 5-col `index_elements` writes against old schema — same `there is no unique or exclusion constraint...` error. Without an explicit pre-migrate gate, the deploy succeeds but every subsequent NAV write raises.

**Verdict:** Real, applies whenever `scripts/deploy/macmini-prod.sh` is invoked. The existing flow does container restart + migrator; the order is correct but the runbook should make it explicit.

**Fix applied:** Task 0 Step 3a documents the (1) stop containers → (2) run migrator → (3) verify constraint → (4) start containers sequence. Also adds a `psql` verification step.

### A3 (HIGH) — Downgrade fails on coexisting source rows

**Where:** Task 0 Step 3a — `downgrade()`.

**Attack:** After Task 0 ships, intraday + close rows coexist for many dates. If a critical bug surfaces and operators run `alembic downgrade -1`, the downgrade tries to recreate the old PK `(broker, account_env, broker_account, date)`. Duplicate-key violation because many dates now have both `intraday` and `close` rows for the same (broker, account_env, broker_account, date).

**Verdict:** Real, locks operators out of fast rollback. They'd have to manually DELETE rows before the downgrade could complete — exactly when they're under pressure during an incident.

**Fix applied:** `downgrade()` now DELETEs close rows for any (scope, date) that has both — preserves intraday (closer to v1 behavior). Cleanup runs in the same transaction as the constraint changes.

## Medium / informational findings (applied as notes, no code change)

### A4 (MEDIUM) — Mid-session retroactive headline change

**Where:** `/performance` page after 17:30 ET Flex fire.

**Attack:** A user opens `/performance` at 16:00 ET. The summary shows `simple_total_return = +5.0%` (computed against intraday rows for past dates). At 17:30 ET, close rows land for today AND backfill for any missing past dates. User refreshes after cache expiry — sees `+5.05%`. The chart for past dates also shifts slightly because close > intraday.

**Verdict:** Real UX behavior, but it's the **audit-correct** direction. Close NAV IS authoritative. The surprise is in the silent change vs the user's mental model.

**Documented in:** Task 7 (`PerformanceHeadlineTooltip`) — add a small "Last refreshed at HH:MM (sources: intraday/close)" subtitle so the user can see WHEN the underlying data changed. Not blocking ship.

### A5 (LOW) — Cache-key staleness across schema migration

`perf_cache` may hold YTD results computed before the migration applies. Post-migration, the cached values are slightly off until TTL expiry. Self-healing within 15 min (cache TTL). No action needed beyond a note in the runbook to clear the cache after migration.

### A6 (MEDIUM) — `_prev_nav` query / `upsert_nav_async` write transaction isolation

`persist_futu_nav` reads `prev_nav` in one async transaction, then writes via `upsert_nav_async` which opens its own. Theoretical inconsistency window if something modifies the previous-day row between the read and the write.

**Verdict:** In practice not exploitable — FUTU has a single sync writer (`persist_futu_nav` is invoked from the FastAPI `/futu/sync` route which serializes via the asyncio singleflight lock). Document as a known-limitation comment in the code; revisit if a parallel FUTU writer is introduced.

## Non-findings (probed, no issue)

- **Concurrent same-source same-date writes:** post-migration PK includes source. Two intraday writes target the same key; `on_conflict_do_update` handles idempotently. ✓
- **Concurrent intraday vs close writes:** different `source` values → different PK rows → no conflict. ✓
- **Network split during Flex poll:** `fetch_ib_nav_series` partial result → some dates written, rest absent. Reconcile CLI surfaces gaps. No data corruption. ✓
- **Empty/null Flex responses:** CLI exits 1, log line, no PG mutation. ✓
- **IB Flex throttle (1018):** CLI returns None, exit 1. Operator-visible. ✓
- **Secret leakage:** `IB_FLEX_TOKEN` sourced from `.env`, not logged, not exported beyond the wrapper subshell. ✓
- **DST transitions:** `pytz.timezone("America/New_York")` + `datetime.now(et).date()` is DST-safe. ✓
- **LaunchAgent + manual kickstart race:** both run the same CLI; both UPSERT idempotently. ✓
- **CI guard bypass via dynamic import:** `pg_insert(nav_history)` is a literal string match; an attacker could use `pg_insert(getattr(schema, 'nav_history'))`. Acceptable risk — this is a static-analysis guard, not security boundary.

## Standing-rule sweep

- ✅ All four CLAUDE.md rules upheld (Yahoo, naked-short, secrets, DB-first, uv).
- ✅ Broker scope discipline preserved across writer migrations.

## Summary

3 critical findings applied inline to plan; 3 medium/low findings documented. No items remaining unaddressed. Pass-3 done.

## Deferred to Pass 4 (final cumulative self-review)

- Re-read the plan end-to-end with all amendments in place; confirm no internal inconsistencies introduced by the 3 amendment passes.

## Deferred to Pass 5 (assumption verification gate)

- LaunchAgent fires correctly under macmini's TZ when system clock is UTC.
- `_build_upsert_stmt` helper composition works correctly when `source=None` is passed (legacy callers).
- `async_engine` fixture creates per-test instances that don't share state across tests in the same session.
