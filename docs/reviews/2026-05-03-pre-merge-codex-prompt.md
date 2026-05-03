# Pre-Merge Review Prompt — Codex (PG Cutoff Branch)

> Paste the section under "Prompt to Codex" verbatim (or via `codex exec -s read-only`).

## Context for human

**Branch:** `feat/pg-cutoff-prereq-fixtures` (10 commits ahead of `master`)

**The simple goal in two sentences.** Cut Xenon over to Postgres as the
single runtime source of truth: no `data/*.json` runtime fallbacks, no
silent dual paths, no fake-account scope leaks. Then ship the runtime to
the remote Mac mini (`192.168.50.47`) so this Mac becomes a pure dev
workspace.

**Authoritative specs.** Codex must read these before judging the diff:

1. `docs/plans/2026-05-03-pg-migration-clean-cutoff.md` — the plan being
   implemented.
2. `docs/plans/2026-05-03-pg-migration-completion-and-remote-deploy.md` —
   the next-phase deploy plan (Docker / launchd to Mac mini).
3. `docs/plans/2026-04-28-postgres-migration-completion.md` +
   `…-IMPL.md` — the prior phase whose loose ends this branch closes.
4. `docs/plans/2026-04-28-order-path-regression-prevention.md` — the two
   CI guards (`no_json_fallback_on_order_path.py`,
   `order_path_caller_allowlist.py`).
5. `docs/architecture/production-database-strategy.md` — broker/account
   scope rules and table inventory.
6. `docs/reference/order-path-incident-history.md` — append a row if any
   guarded surface changed semantics.
7. `CLAUDE.md` — § "Local vs. Remote (post-2026-05-03 split)" + § "Mandatory
   Rules" + § "Runtime Data Read Paths".

**Commits in scope (order matters).**

```
8222d267 test(conftest): scope_fixture + pg_test_engine + offline-tolerant PG truncate
3dc17587 feat(db): get_account_snapshots_history sync+async + lazy PG probe
112fe8ea infra(dev): export XENON_BROKER_ACCOUNT per mode from .env
3aa13f9  docs(decisions): exit-orders-handler retire + web-test PG-seed via uv script
fb52100c feat(db): extend nav_history schema with IB Flex breakdown columns
0d702963 chore(monitor): retire ExitOrdersHandler
c49b7e0f feat(ib_sync): replace JSON entry-date fallback chain with PG lookups
2dd3853c refactor(uw): strip JSON fallbacks from 3 UW services — PG only
146cefab fix(pg-cutoff): apply tribunal CRITICAL+IMPORTANT findings on Strip 1-5
6c82511a fix(pg-cutoff): finish tribunal items + tighten allowlist to zero
```

---

## Prompt to Codex

You are reviewing the **`feat/pg-cutoff-prereq-fixtures` branch** of the
Xenon repository before it merges to `master`. **Read-only**. Do not write
files.

### What I want from you

Two things, and only these two things:

1. **Spec conformance.** For each requirement in the authoritative spec
   list (below), state whether the diff satisfies it (`✓ MET`),
   partially satisfies it (`◐ PARTIAL — what's missing`), or violates it
   (`✗ VIOLATED — what's wrong + which file:line`). Cite spec sections.

2. **Goal alignment.** The simple goal is "**clean cutoff of postgres
   and json, then ship to macmini server.**" For each commit in scope,
   answer: does this commit move toward the goal, away from it, or is it
   neutral? If a commit ADDS dual-path code, fake fallbacks, or silent
   error swallowing that re-introduces the kind of regression this
   branch is supposed to delete, flag it as a SHIP-BLOCKER.

### Authoritative specs to read FIRST

```
docs/plans/2026-05-03-pg-migration-clean-cutoff.md
docs/plans/2026-05-03-pg-migration-completion-and-remote-deploy.md
docs/plans/2026-04-28-postgres-migration-completion.md
docs/plans/2026-04-28-postgres-migration-completion-IMPL.md
docs/plans/2026-04-28-order-path-regression-prevention.md
docs/architecture/production-database-strategy.md
docs/reference/order-path-incident-history.md
CLAUDE.md
src/xenon/CLAUDE.md
src/xenon/api/CLAUDE.md
```

### Diff scope

```bash
git log --oneline origin/master..HEAD
git diff origin/master..HEAD
```

10 commits. Touch points:

- DB schema + alembic migrations:
  `src/xenon/db/schema.py`,
  `src/xenon/db/migrations/versions/2026_05_03_extend_nav_history_breakdown.py`,
  `src/xenon/db/migrations/versions/2026_05_03_uw_snapshots_cache_state.py`
- DB queries (sync + async):
  `src/xenon/db/queries/portfolio.py`,
  `src/xenon/utils/portfolio_loader.py`
- Order path:
  `src/xenon/execution/ib_sync.py`,
  `src/xenon/execution/naked_short_audit.py`
- UW services:
  `src/xenon/api/services/uw_analyze_cache.py`,
  `src/xenon/api/services/uw_analyze_flow_tracker.py`,
  `src/xenon/utils/uw_api_stats.py`,
  `src/xenon/api/server.py`
- Reports:
  `src/xenon/reports/portfolio_performance.py`
- CI guards:
  `scripts/checks/no_json_fallback_on_order_path.py`
- Dev launcher:
  `scripts/infra/dev.sh`
- Tests:
  `scripts/tests/test_uw_analyze_cache.py`,
  `scripts/tests/test_uw_analyze_preload.py`,
  `scripts/tests/test_portfolio_performance.py`,
  `src/xenon/api/tests/conftest.py`

### Spec-conformance checklist (give me one ✓/◐/✗ per item)

**A. Clean-cutoff (the simple goal — left half)**
A1. **No new `data/*.json` runtime reads** in production code under
`src/xenon/api/`, `src/xenon/execution/`, `src/xenon/reports/`,
`src/xenon/utils/`. Comments/docstrings referencing the removed
files for historical context are OK.
A2. **`_ALLOWLIST` in `scripts/checks/no_json_fallback_on_order_path.py`
is empty.** Any allowlist re-population is a violation.
A3. **`order_path_caller_allowlist.py` still passes** with the diff. The
callers of `xenon.execution.ib_place_order` should still be only the
documented set (server.py, the module itself, tests).
A4. **NAV writer/reader in `portfolio_performance.py`** writes
`xenon.nav_history` via `upsert_nav_sync` (with breakdown columns)
and reads via `load_nav_history_sync`. No `data/nav_history_ib.json`
or `data/nav_history.jsonl` write OR read on the production path.
A5. **Gate-4 audit** in `ib_sync.py` (post-sync naked-short check) reads
open orders from the live IB session, NOT from `data/orders.json`.
A6. **`naked_short_audit.py` CLI** defaults to PG for positions and live
IB for open orders. The `--portfolio FILE` and `--orders FILE` flags
remain as opt-in test/forensic overrides only.
A7. **Entry-date PG lookup** (`load_entry_date_lookups_sync`) is wrapped
in try/except inside `ib_sync.convert_to_portfolio_format` so a PG
outage degrades to "unknown" instead of aborting the whole sync.
A8. **`upsert_nav` / `upsert_nav_sync`** are NULL-safe on every nullable
column (daily_pnl, total, cash, stock_value, options_value): a
caller passing `None` must NOT clobber an existing PG value.

**B. Schema + migrations**
B1. **Single alembic head.** `uv run alembic heads` returns exactly
one revision (`9f2c4a1d8e57`).
B2. **`xenon.nav_history` extended** with `total`, `cash`, `stock_value`,
`options_value` (all nullable).
B3. **`xenon.uw_analyze_snapshots` extended** with `sources`,
`oi_baseline`, `previous_snapshot` (all nullable JSONB). Internal
cache state survives restart, so source-aware eviction priority and
OI diffing are not silently broken.
B4. **`UwAnalyzeCache._ensure_loaded`** restores all three new fields
into the in-memory entry shape (current.ticker, sources,
oi_baseline, previous).
B5. **`UwAnalyzeCache._archive_to_postgres`** writes all three new
fields on every refresh.

**C. UW service hardening (Codex tribunal items)**
C1. **`FlowLog._loaded`** is set to True ONLY after a successful PG
load. PG flap on the first call must NOT latch the FlowLog as empty.
C2. **`FlowLog.purge()`** issues a real `DELETE FROM xenon.uw_flow_events`
on the purged keys. Restart must NOT resurrect purged events.

**D. Account-scope hygiene**
D1. **`scripts/infra/dev.sh`** requires `XENON_PAPER_ACCOUNT` for paper
mode (no `DU0000000` fallback). Errors out with a clear message
referencing the clean-slate principle if unset.
D2. **`AccountScope` calls in subprocesses** use `resolve_from_env()`
(strict) — confirm no new lax `_scope_from_env()` callers were
introduced.

**E. Tests + CI**
E1. **`api/tests/conftest.py` truncate** includes `xenon.order_fills`
(Codex tribunal #10 — orphan fills were leaking across tests).
E2. **`test_uw_analyze_preload.py`** seeds `xenon.uw_analyze_snapshots`
via `pg_test_engine` (no longer monkeypatches the deleted
`_DEFAULT_CACHE_PATH` constant).
E3. **Tests against deleted disk-archive code** (`cache.history_path`,
`_archive_to_disk`) are skipped with a clear reason pointing at the
PG cutoff. They are NOT silently passing on broken assertions.

**F. Goal alignment — ship to Mac mini (the simple goal — right half)**
F1. **Nothing in this branch hard-codes the dev Mac as the runtime
host.** Anything that resolves IB or PG hosts should resolve from
env vars (`IB_GATEWAY_HOST`/`IB_GATEWAY_PORT`/`DATABASE_URL`) per
`dev.sh paper|live`.
F2. **No new code paths assume `data/` exists.** A fresh Mac mini with
an empty repo and an empty `core` PG should be able to boot
FastAPI + ib_sync + naked_short_audit and serve the relevant
endpoints (modulo the documented residual reads: `watchlist.json`,
`futu_portfolio.json`, `flex_token_config.json`).
F3. **The `Local vs. Remote (post-2026-05-03 split)` table in
`CLAUDE.md`** still describes the truth after this branch.

### How to format your response

Open with the bottom line in one short paragraph: **SHIP** or **DO NOT
SHIP**, and the single most important reason. Then sections A–F as
checklists with `✓`, `◐`, or `✗` next to each item, file:line cites for
violations.

End with two lists:

- **Ship-blockers** — items that should be fixed before merging.
- **Follow-ups** — items that can ship in this PR but should land
  immediately after as separate commits (e.g., `data/watchlist.json`
  migration, `data/futu_portfolio.json` Futu rethink, Phase-1 Docker
  work from `…-completion-and-remote-deploy.md`).

If anything in the diff conflicts with the cited specs, name the
conflict precisely. If the spec is silent on something the diff does,
say so — silence is not approval.

**Hard constraint**: do not flag pre-existing issues in unchanged code.
Stay inside the diff and the spec.
