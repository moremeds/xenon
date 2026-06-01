# Performance Rebuild — Execution Evidence (2026-06-01)

Branch: `feat/perf-rebuild` (off `master @ 0c82967`)
Worktree: `.worktrees/perf-rebuild-impl/`
Spec: `docs/superpowers/specs/2026-05-31-performance-rebuild-design.md` (v3.1) — on `docs/performance-rebuild-spec` branch in sibling worktree
Plan: `docs/superpowers/plans/2026-06-01-performance-rebuild.md` (with PRE-EXECUTION CORRECTIONS section) — same sibling worktree

---

## What was produced (review phase, fully verified)

### 1. Spec v3.1 patches (3 design corrections)

| #   | Patched section                      | Evidence                                                                                                                                    |
| --- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | §8 IB `dailyPnL` cash-flow semantics | Demoted to UNVERIFIED ASSUMPTION; Phase 0 verification gate added (spec §Phase 0)                                                           |
| 2   | §10 `_matched_acc` does not exist    | Replaced with `_matched_trd_env` attribute + `trd_env_of_matched_account()` accessor; also fixed silent-env-lie bug at `futu_client.py:142` |
| 3   | §4 Threshold ladder                  | Added low-confidence indicator for 30 ≤ n < 126 with Sharpe SE math; env-tunable via `XENON_PERF_LOW_CONFIDENCE_DAYS`                       |

Commit: spec worktree `233db01 docs(perf): plan v1 + spec v3.1 patches + tribunal pre-execution corrections`

### 2. Implementation plan + 30-issue corrections block

| Artifact                                                   | Lines    | Status    |
| ---------------------------------------------------------- | -------- | --------- |
| `docs/superpowers/plans/2026-06-01-performance-rebuild.md` | ~3400    | committed |
| PRE-EXECUTION CORRECTIONS (section at top of plan)         | 30 items | committed |

Reviews that produced corrections:

- Self-review (5 inline gaps fixed during plan write)
- Codex CLI (`gpt-5.3-codex`, 15 issues)
- Claude integration audit (15 issues, via Explore agent)
- Adversarial agent (15 new failure modes, general-purpose)

Total unique issues catalogued: 30 (17 critical, 6 high, 7 medium)

### 3. Confidence assessment delivered

Pre-execution: 85% (residual 15% = empirical Phase 0, multi-worker uvicorn, IB pool integration, E2E browser).

---

## What was executed (Phase 1 — schema)

### Commit: `891e649 feat(perf-rebuild): Phase 1 schema + correction #1`

| Artifact                                                                                                                                                     | Evidence to verify                                                                 | Status                   |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------- | ------------------------ |
| `src/xenon/utils/market_calendar.py::current_session_date_et()` (correction #1)                                                                              | `uv run pytest scripts/tests/test_current_session_date_et.py` → 2 pass             | ✅ verified              |
| `src/xenon/db/migrations/versions/ed8820930349_add_benchmark_closes_table.py`                                                                                | `uv run alembic heads` → `489476c351cc` is current head                            | ✅ code ready            |
| `src/xenon/db/migrations/versions/260fabba18d6_add_nav_history_source_column.py`                                                                             | Same                                                                               | ✅ code ready            |
| `src/xenon/db/migrations/versions/489476c351cc_add_nav_history_one_env_per_day_...py`                                                                        | Same                                                                               | ✅ code ready            |
| `src/xenon/db/schema.py` — `nav_history.source` Column + `benchmark_closes` Table + `Index("nav_history_one_env_per_day", ..., unique=True)` (correction #3) | Schema imports `Table`/`Column`/`xenon_metadata` matching local style              | ✅ verified by code-read |
| `scripts/tests/test_schema_perf_rebuild.py` (5 tests covering all 3 migrations)                                                                              | Tests written but require Postgres test DB (DATABASE_URL_TEST not set in this env) | ⚠️ pending DB            |

### Verification commands (re-runnable)

```bash
# Worktree
cd /Users/moremeds/projects/xenon/.worktrees/perf-rebuild-impl

# Unit tests
uv run pytest scripts/tests/test_current_session_date_et.py -v
# → 2 passed

# Migration metadata
uv run alembic heads
# → 489476c351cc (head)

# Branch diff
git log master..HEAD --oneline
# → 891e649 feat(perf-rebuild): Phase 1 schema + correction #1
```

---

## Update — Phases 2 + 3 also executed (commits `4d68e9e`, `4f5cfa6`)

After the user authorized a separate local Postgres instance (port 2000, no
conflict with the Docker stack), all 3 migrations applied cleanly, and Phases
2 + 3 landed with full test coverage.

### Phase 2 — Backend foundations (24 tests, 8.2s)

| Artifact                                                                                                | Tests                                     | Status |
| ------------------------------------------------------------------------------------------------------- | ----------------------------------------- | ------ |
| `src/xenon/clients/futu_client.py` — `_matched_trd_env` + accessor + module-level futu imports          | `test_futu_client_matched_trd_env.py` × 4 | ✅     |
| `src/xenon/execution/account_scope.py` — `env_from_trd_env` + FUTU rejection (SIMULATE→"paper" per #18) | `test_account_scope_env_helpers.py` × 10  | ✅     |
| `src/xenon/api/services/futu_nav_persistence.py` — race-safe cross-env guard                            | `test_futu_nav_persistence.py` × 8        | ✅     |
| `src/xenon/execution/ib_sync.py::_append_nav_snapshot` — symmetry guard                                 | `test_ib_sync_cross_env_guard.py` × 2     | ✅     |

### Phase 3 — Service + queries + cache + metrics (42 tests, 9.8s)

| Artifact                                                                                                                                 | Tests                              | Status |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------- | ------ |
| `src/xenon/reports/performance_metrics.py` — pure math + `sharpe_se`                                                                     | `test_performance_metrics.py` × 16 | ✅     |
| `src/xenon/db/queries/nav_history.py` — `load_nav_curve` + `load_benchmark_cached` (cache-only path live; IB-fetch gated behind env)     | `test_nav_history_queries.py` × 6  | ✅     |
| `src/xenon/api/services/performance.py` — `compute()` with Phase-0 mask + low-confidence + date-join benchmark + correct returns formula | `test_performance_service.py` × 12 | ✅     |
| `src/xenon/api/services/perf_cache.py` — scope-keyed memoize + market-aware TTL                                                          | `test_perf_cache.py` × 8           | ✅     |

**Full regression: 73/73 tests passing in 18s.**

Verification commands:

```bash
cd /Users/moremeds/projects/xenon/.worktrees/perf-rebuild-impl
DATABASE_URL_TEST="postgresql+asyncpg://xenon_app:xenon_dev@localhost:2000/core_dev" \
  uv run pytest scripts/tests/test_{current_session,schema_perf,futu_client_matched,account_scope_env,futu_nav_persistence,ib_sync_cross_env,performance_metrics,nav_history_queries,perf_cache,performance_service}*.py
# → 73 passed
```

### Local DB setup (port 2000, isolated from Docker stack)

```bash
docker run -d --name xenon-perf-test-pg -p 2000:5432 \
  -e POSTGRES_USER=xenon_app -e POSTGRES_PASSWORD=xenon_dev \
  -e POSTGRES_DB=core_dev postgres:15
# Schema bootstrap:
DATABASE_URL="postgresql+psycopg://xenon_app:xenon_dev@localhost:2000/core_dev" \
  uv run python -c "import sqlalchemy as sa,os; e=sa.create_engine(os.environ['DATABASE_URL']); \
    e.begin().__enter__().execute(sa.text('CREATE SCHEMA IF NOT EXISTS xenon')); \
    e.begin().__enter__().execute(sa.text('CREATE SCHEMA IF NOT EXISTS events'))"
# Apply all migrations (15 baseline + 3 perf-rebuild):
DATABASE_URL="postgresql+asyncpg://xenon_app:xenon_dev@localhost:2000/core_dev" \
  uv run alembic upgrade head  # → 489476c351cc (head)
```

---

## What was BLOCKED (and why)

### Blocker 1: Migration 489476c351cc cannot apply to `core_dev` (shared DB)

**Finding:** A pre-existing cross-env collision exists in `xenon.nav_history`:

| broker | broker_account | date       | account_envs        |
| ------ | -------------- | ---------- | ------------------- |
| IB     | DUQ378889      | 2026-04-27 | `['live', 'paper']` |

The unique-index migration aborts with `UniqueViolationError` because the historical data has 2 rows for the same `(broker, broker_account, date)` with different envs. This is EXACTLY the failure mode the index is meant to prevent — but the bug was already in the data before this PR.

**Why I stopped:** Per project + global CLAUDE.md "Executing actions with care": _destructive operations on shared infra need explicit user authorization_. Deleting a `nav_history` row is destructive and shared.

**Resolution options (operator chooses):**

a. **Delete the older/incorrect row** (likely the `paper` one if the account was always live, or vice versa) — needs you to confirm which env is correct for `DUQ378889 on 2026-04-27`. Then migration applies cleanly.

b. **Resolve to one env, then run the migration**, but back up first:

```bash
psql -h 192.168.50.47 -U xenon_app core_dev -c \
  "BEGIN; \
   INSERT INTO xenon.nav_history_backup_20260601 SELECT * FROM xenon.nav_history WHERE broker_account='DUQ378889' AND date='2026-04-27'; \
   DELETE FROM xenon.nav_history WHERE broker_account='DUQ378889' AND date='2026-04-27' AND account_env='<choose: paper or live>'; \
   COMMIT;"
```

Then `DATABASE_URL=... uv run alembic upgrade head` from the worktree.

c. **Defer migration #3** for a follow-up PR after data cleanup. Migrations #1 and #2 (additive, non-destructive) can apply now.

### Blocker 2: Phases 2–7 not executed in this session

Reasons:

- **Phase 2** (FutuClient fix + persist_futu_nav + ib_sync guard): code work is straightforward but the test fixtures depend on the schema migrations applying first (blocked by #1 above) AND on adding the `async_engine`/`sync_engine` fixtures per correction #6.
- **Phase 3** (services + queries): correctness depends on the IB pool surface (correction #9 — `with_role`/`contract_for` don't exist; needs rewrite); benchmark fetch will fail at runtime without that fix.
- **Phase 4** (FastAPI wiring): needs Phases 2–3 done first.
- **Phase 5** (web): independent of Postgres but needs Phase 4 routes mounted to verify end-to-end.
- **Phase 6** (deprecate CLIs): trivial — can run anytime.
- **Phase 7** (E2E browser): needs full stack (IB Gateway + Postgres + dev server) — not started in this session.
- **Phase 0** (verify IB `dailyPnL`): inherently empirical — needs a paper IB account + 1 trading day.

---

## What's verified (cross-checked)

| Claim                                                                            | Verification method                                                                                         |
| -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `current_session_date_et()` exists and returns ET date                           | `uv run pytest scripts/tests/test_current_session_date_et.py -v` — 2 passed                                 |
| Migration #1 + #2 + #3 generate valid Alembic scripts                            | `uv run alembic heads` → `489476c351cc (head)` (parses, registers)                                          |
| Schema reflections use correct local imports (`Table`/`Column`/`xenon_metadata`) | Read `src/xenon/db/schema.py:174-217` — `benchmark_closes = Table("benchmark_closes", xenon_metadata, ...)` |
| Spec §10 mapping is `SIMULATE→"paper"` (aligned with IB)                         | Read spec line 55 — correction #18 applied                                                                  |
| 30 review issues catalogued in plan's corrections block                          | Plan worktree commit `233db01` — section "PRE-EXECUTION CORRECTIONS"                                        |
| Phase 1 commit landed on `feat/perf-rebuild`                                     | `git log master..HEAD` → `891e649`                                                                          |
| `_append_nav_snapshot` actual signature is `(net_liq, daily_pnl=None)`           | `sed -n '1031,1035p' src/xenon/execution/ib_sync.py` — confirms; correction #2 documented                   |
| `app.state.account` (not `broker_account`) is the IB account field               | `grep "app.state.account\|broker_account" src/xenon/api/server.py` — only `account` present; correction #4  |
| `IBPool` exposes `.get(role)` not `with_role/contract_for`                       | `grep "def " src/xenon/api/ib_pool.py` — only `get` method; correction #9                                   |
| Cross-env collision exists in `core_dev` (the blocker)                           | SQL query against `xenon.nav_history` GROUP BY HAVING count(distinct account_env) > 1 → 1 row               |

## What's NOT verified (would need follow-up)

| Claim                                                                      | Why not verified                                                   |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Migrations #1 and #2 apply cleanly                                         | All-or-nothing alembic upgrade; #3 aborted the batch (rolled back) |
| Test fixtures `async_engine`/`sync_engine` (correction #6)                 | Not added yet — Phase 2+ tests would need them                     |
| FutuClient `_matched_trd_env` behavior in connect-fallback                 | Test code drafted in plan but not implemented — Phase 2            |
| `persist_futu_nav` cross-env race correctly raises `NavAccountEnvConflict` | Phase 2.3 code not implemented yet                                 |
| IB `dailyPnL` cash-flow semantics                                          | Phase 0 empirical test — needs paper IB + 1 trading day            |
| End-to-end UI flow (IB ↔ Futu tab switch)                                  | Phase 7 — needs full stack                                         |

---

## Recommended next session

1. **Operator decision on Blocker 1** — resolve `(IB, DUQ378889, 2026-04-27)` env collision; back up the row first.
2. **Apply migrations** — `DATABASE_URL=... uv run alembic upgrade head` from the worktree.
3. **Continue Phase 2** — start with fixture additions to `scripts/tests/conftest.py` (correction #6), then Task 2.1 FutuClient fix (correction #8 — refactor `OpenSecTradeContext` to module-level import first, then implement `_matched_trd_env`).
4. **Defer Phase 0** until paper account + trading day are available; ship Phases 1–6 with `XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS=true` (safe default — masks IB metrics until verified).
