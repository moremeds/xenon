# XENON — CLAUDE.md

Master policy file. Topic-specific guidance lives in subdirectory `CLAUDE.md` files:

| Area                                             | File                      |
| ------------------------------------------------ | ------------------------- |
| Frontend, pricing, P&L                           | `web/CLAUDE.md`           |
| Python execution, reports, IB/Futu CLIs          | `src/xenon/CLAUDE.md`     |
| FastAPI, Clerk auth, IB Gateway, order lifecycle | `src/xenon/api/CLAUDE.md` |
| Brand tokens, typography, spectrum, UI rules     | `brand/CLAUDE.md`         |

## Brokers

- **IB** (primary) — quotes, chains, execution, portfolio. Never bypassed.
- **Futu** (read-only) — positions snapshot via local Futu OpenD. Surfaces as a separate account tab. No orders, no fills, no quotes. Silent degrade when OpenD unreachable.

Every execution/portfolio row carries `broker`, `account_env`, `broker_account` columns so paper and live data never blend in a shared Postgres. Resolve scope via `AccountScope` (`src/xenon/execution/account_scope.py`); FastAPI depends on `get_account_scope`, sync subprocesses read `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`. Full policy: `docs/architecture/production-database-strategy.md` § Broker Account Scope.

## Portfolio Structure Classification

- Preserve recognized multi-leg structures: verticals, straddles, synthetics, covered calls, risk reversals, butterflies/condors where classified.
- Do not collapse unrelated two-leg same-symbol/same-expiry singles into `Combo (2 legs)` when structure detection falls through. Split that narrow fall-through back into concrete single-leg rows (`Short Put`, `Long Call`, `Stock`, etc.) so row-level order actions stay available.
- Keep 3+ leg fall-through combos intact unless there is explicit order lineage or a recognized structure; splitting unknown complex orders can misrepresent user intent.
- IB position snapshots do not carry originating combo order id. If exact lineage is needed, join against order submission records instead of guessing from symbol/expiry alone.

## Data Sources

1. **Interactive Brokers** — real-time quotes, chains, execution, live portfolio. Primary.
2. **Futu OpenD** — read-only positions snapshot for the Futu account tab.
3. **Unusual Whales (`$UW_TOKEN`)** — historic OHLC + option contract history for `xenon-portfolio-perf` and `xenon-portfolio-report`. Not used for signal generation (removed in the pure-portfolio pivot).
4. **IB Flex Query** — historical fills audit overlay (`xenon-blotter-history`).

**Never use Yahoo Finance.**

## Runtime Data Read Paths

Postgres is the runtime source of truth for portfolio, orders, fills, and journal surfaces. Do not reintroduce silent file fallbacks.

- Portfolio UI reads `xenon.account_snapshots.payload` through FastAPI `GET /portfolio`, scoped by `AccountScope` (`XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`). `data/portfolio.json` is legacy/backfill input only, not a UI read path.
- Orders, fills, journal, attribution surfaces read from `xenon.order_submissions`, `xenon.order_fills`, `xenon.trades`, `xenon.journal_entries` via FastAPI routes.
- Next.js API routes proxy these surfaces via `xenonFetch()`; no `readFile`, `readDataFile`, or cached JSON fallback on runtime paths.

## Dev/Prod DB Split

Two databases on the macmini Postgres, written by two distinct roles. **Everything writes to Postgres first; the website only reads.** Full policy + role/grant SQL: `docs/architecture/production-database-strategy.md` § Dev/Prod DB Split. Operator cutover steps: `docs/runbooks/dev-prod-db-cutover.md`.

| DB          | Role         | Writers                                                 | Refresh                                                                                                              |
| ----------- | ------------ | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `core_dev`  | `xenon_prod` | macmini Docker stack (api, migrator, realtime services) | source of truth                                                                                                      |
| `core_test` | `xenon_dev`  | MacBook dev sessions via `scripts/infra/dev.sh paper`   | `pg_dump core_dev → pg_restore core_test` nightly at 04:00 ET (`scripts/infra/refresh-core-test.sh` via LaunchAgent) |

Defense in depth — three layers prevent dev sessions from touching prod:

1. **PG role grants** — `xenon_dev` has no write permission on `core_dev`; `xenon_prod` is the only role granted INSERT/UPDATE/DELETE there, with `ALTER DEFAULT PRIVILEGES` so new tables inherit the ACL.
2. **`dev.sh` startup guard** — refuses to boot when `DATABASE_URL` parses to `core_dev`, regardless of the role. Test: `scripts/tests/test_dev_sh_db_guard.py`.
3. **CI write-side guard** — `scripts/checks/no_json_write_on_order_path.py` fails new `writeFile` / `Path(...).write_text` / `_atomic_save` against `data/*.json` in `web/app/api/`, `web/lib/order/`, `src/xenon/api/`, `src/xenon/execution/`. Mirror of the existing read-side guard; locks in DB-first as a CI invariant.

### `dev.sh live` is read-only

`scripts/infra/dev.sh live` exports `XENON_READ_ONLY=1`. The flag forces a hard refusal on every write surface so a dev session against live IB never pollutes `core_test` with live fills:

- `POST /orders/{place,cancel,modify}` and `POST /journal` return 403 with top-level `reason_code: READ_ONLY_MODE` (see `src/xenon/api/guards.py::read_only_403`; the toast helper reads `body.reason_code`, not `body.detail.reason_code`).
- `ib_sync._save_portfolio_to_postgres` and `_append_nav_snapshot` are no-ops.
- FastAPI lifespan skips `_run_rehydrate_on_boot`, `_run_fills_replay_on_boot`, and `_maybe_start_activity_poller`.

Real live trading must go through the macmini Docker stack, which writes `core_dev`. Tests: `src/xenon/api/tests/test_read_only_mode.py`, `scripts/tests/test_ib_sync_read_only.py`.

## Order-Path Guards (Layers 1+2)

**Read first:** `docs/reference/order-path-incident-history.md` — chronological log of every non-trivial order-path bug, root cause, fix, and regression test. Append a row when you ship a similar fix.

Three automated guards lock in regression patterns. Original two from PR #61; the write-side guard ships with the dev/prod DB split. Full design: `docs/plans/2026-04-28-order-path-regression-prevention.md`.

- **Edit-time reminder** (`.claude/hooks/order-path-reminder.sh`) — PreToolUse hook prints an order-path checklist when Claude edits files under `src/xenon/execution/`, `src/xenon/api/server.py`, `web/app/api/orders/`, or `web/lib/order/`. Advisory, never blocks.
- **CI guards** (`.github/workflows/ci.yml::order-path-guards`):
  - `scripts/checks/no_json_fallback_on_order_path.py` — fails new `readDataFile`/`json.load` against `data/*.json` in `web/app/api/` or `src/xenon/api/`. Existing legacy reads are pinned in `_ALLOWLIST`; the list is intended to shrink to zero.
  - `scripts/checks/no_json_write_on_order_path.py` — write-side mirror. Fails new `writeFile` / `Path(...).write_text` / `_atomic_save` against `data/*.json` in `web/app/api/`, `web/lib/order/`, `src/xenon/api/`, `src/xenon/execution/`. Enforces the DB-first principle from § Dev/Prod DB Split.
  - `scripts/checks/order_path_caller_allowlist.py` — fails imports/CLI invocations of `xenon.execution.ib_place_order` outside the allowlist (`server.py`, the module itself, tests). Locks down [In-process route bypass].
- **Local pre-commit** (optional): `scripts/checks/install-pre-commit.sh` drops a managed `.git/hooks/pre-commit` that runs all three guards.

To audit the current allowlists: `python3 scripts/checks/no_json_fallback_on_order_path.py --show-allowlist` (and the corresponding flags on the write-side and caller guards).

## ⛔ Mandatory Rules

1. **Be concise.** No preamble, no filler.
2. **E2E browser verification for ALL UI work.** Primary: `chrome-cdp`. Fallback: Playwright (`web/playwright.config.ts`). No UI change done until visually confirmed. Don't assume code changes produce the expected visual result — verify rendered output in the browser before committing.
3. **Red/green TDD for ALL code.** Failing test → fix → green → refactor. Unit: Vitest, E2E: chrome-cdp/Playwright.
4. **95% test coverage target.** Every change includes corresponding tests.
5. **API keys** in `.env` files (see Credentials below). Fallback: `~/.zshrc`.
6. **Options structure reference:** `docs/trading/options-structures.json` + `docs/trading/options-structures.md` — 58 structures, guard decisions, P&L attribution labels. Use for order entry, structure classification, and naked short guard logic.
7. **Todo capture.** When the user says "todo" (e.g. "todo: explore X", "add this as a todo"), append the idea to the **Inbox** section at the bottom of `docs/todo-backlog.md` with today's date. Do not start work on it. Do not silently drop it. The backlog is a queue for future planning sessions, not active work. **Add your own commentary** under each entry — hypotheses, suspected file sites, dependencies on other backlog items, design questions, references to commits/CLAUDE.md guidance — anything that would save future-you from rebuilding the context from scratch. Use a `**Notes:**` sub-bullet to keep your commentary visually separate from the user's original framing.

## Identity

**Xenon** — broker terminal for options portfolio management. Live IB + read-only Futu integration. Places orders, tracks fills, enforces position-close rules, surfaces P&L and Greeks attribution. **No signal generation, no scanner output — bring your own thesis.**

Brand spec: `brand/CLAUDE.md` + `docs/reference/brand-identity.md`.

## ⛔ Naked-Short Guard — Mandatory, No Exceptions

The system must never allow naked short exposure. Every short call must be covered by long shares (1 contract = 100 shares) or by long calls at the same expiry. Cash-secured puts are allowed. Combos that net to uncovered short calls are blocked at three layers: UI pre-submission (`web/lib/nakedShortGuard.ts`), API gate (`/api/orders/place` returns 403), and post-sync audit (`naked_short_audit.py` cancels violators after every `ib_sync`).

Full enforcement matrix and combo logic: `src/xenon/CLAUDE.md` § Naked Short Protection.

## Credentials

| File          | Loader          | Contains                                                                                                                                                                                                                     |
| ------------- | --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.env` (root) | `python-dotenv` | `DATABASE_URL`, `DATABASE_URL_TEST`, `CLERK_JWKS_URL`, `CLERK_ISSUER`, `ALLOWED_USER_IDS`, `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `IB_FLEX_TOKEN` (optional), `IB_FLEX_QUERY_ID` (optional) |
| `web/.env`    | Next.js         | `ANTHROPIC_API_KEY` (chat), `UW_TOKEN` (portfolio reports), `EXA_API_KEY` (optional ticker-page enrichment), `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`                                                         |

`IB_FLEX_TOKEN` + `IB_FLEX_QUERY_ID` are optional. When unset, the Historical Trades panel renders an empty state with a setup hint instead of erroring; `xenon-blotter-history` and `POST /blotter` exit gracefully with `configured=false`. Set both to enable IB Flex Query as an audit overlay (see `uv run python -m xenon.trade_blotter.flex_query --setup`).

## Market Hours

```bash
TZ=America/New_York date +"%A %H:%M"   # 9:30–16:00 ET, Mon–Fri
```

Quotes, fills, and position updates flow whenever IB Gateway is connected. Outside RTH the portfolio still renders from the last `account_snapshots.payload`; flag stale data in the UI rather than swap to a different source.

## Startup Checklist

- [ ] `scripts/infra/dev.sh paper` (paper IB on local `127.0.0.1:4002`, writes `core_test`) — OR `scripts/infra/dev.sh live` (live IB on macmini `100.66.147.98:4001`, **read-only — `XENON_READ_ONLY=1`**)
- [ ] Dev stack binds **3200** (Next), **8421** (FastAPI), **8866** (realtime WS). Production launchd keeps 3000/8321/8765; the dev ports are offset so the xenon dev stack coexists with the local radon stack.
- [ ] If cold start: approve 2FA on IBKR mobile
- [ ] `psql -h 100.66.147.98 -U xenon_dev core_test -c "SELECT 1"` — verify Postgres reachable. `dev.sh` refuses to start if `DATABASE_URL` points at `core_dev` (prod).
- [ ] `curl http://localhost:8421/health` — verify `ib_gateway.port_listening: true`
- [ ] Reconciliation auto-runs on lifespan boot (single-leg rehydrate + fills replay) — **skipped under `XENON_READ_ONLY=1`**
- [ ] IB activity poller running (60s default — TWS-side edits + fills mirrored into Postgres) — **skipped under `XENON_READ_ONLY=1`**
- [ ] Check market hours

## Serena (symbol-aware code tools)

Serena MCP is configured and onboarded (5 project memories indexed; python + typescript LSPs). **Use it — don't default to Read/Grep for symbol work.**

Per-session bootstrap (cheap, idempotent):

1. `mcp__plugin_serena_serena__activate_project` → `/Users/chenxi/projects/xenon`
2. `mcp__plugin_serena_serena__check_onboarding_performed` (should already be true)

**Use Serena when:**

- Renaming a symbol used in >1 file → `rename_symbol` (LSP-accurate, beats `grep`/`sed` on false positives in strings/comments)
- Finding all callers before a refactor → `find_referencing_symbols`
- Replacing a whole function/class body → `replace_symbol_body`
- Exploring an unfamiliar module → `get_symbols_overview` + `find_symbol` (depth=1) before reading bodies
- Safe deletion of dead symbols → `safe_delete_symbol` (checks references first)
- Cross-session notes about architecture decisions → `write_memory` / `read_memory`

**Skip Serena (use Read/Edit/Grep) when:**

- Editing a known file at a known location
- Markdown, JSON, YAML, config, or non-code edits
- Single-file scope with no cross-references
- Anything where `grep` is faster than indexing

**Gotchas:**

- Serena line numbers are **0-based** (Read is 1-based — don't mix).
- Symbol edits are trusted (don't re-read to verify — the LSP confirms).
- After any Serena refactor that crosses type boundaries, still run `uv run pytest` + `cd web && npm test` per the usual gates.

## Python: `uv` for everything

**All Python invocations go through `uv`.** Never call bare `python3.13`, `python`, `pip`, or activate `.venv` manually. `uv` resolves the interpreter from `.python-version` + `pyproject.toml` and keeps the lockfile authoritative.

| Action                | Command                                               |
| --------------------- | ----------------------------------------------------- |
| Install / sync deps   | `uv sync --extra test`                                |
| Run a CLI entry point | `uv run xenon-trend-scan ...`                         |
| Run a one-off script  | `uv run python scripts/foo.py`                        |
| Run pytest            | `uv run pytest ...`                                   |
| Add a dep             | `uv add <pkg>` (updates `pyproject.toml` + `uv.lock`) |

CI is consistent: `uv sync --frozen --extra test` → `uv run pytest`. `release.yml` uses the same. If you see `python3.13 ...` anywhere in docs or scripts, treat it as stale — rewrite to `uv run`.

## Tests

Locally:

```bash
uv sync --extra test                                                # one-time / after dep changes
uv run python scripts/infra/dev/run_pytest_affected.py              # scoped Python tests (preferred)
uv run pytest scripts/tests/test_foo.py::test_name -xvs             # single test
uv run pytest                                                       # full suite
cd web && npm test                                                  # Vitest
cd web && npx playwright test                                       # E2E
```

Order-route integration tests use `web/tests/fastapiHarness.ts` with `XENON_API_TEST_MODE` to stub broker calls — no live IB required.

### Pytest infrastructure (3-phase speedup)

The full Python suite is intentionally cheap to run locally. Don't disable or bypass these layers:

- **Phase 1** — session-scoped engine + single `TRUNCATE` reset (`src/xenon/_test_db.py::truncate_all_xenon_tables`).
- **Phase 2** — autouse `BEGIN/ROLLBACK` per test, via `_postgres_orders_test_db` in `scripts/tests/conftest.py`. Each test sees an empty schema; the app engine is monkeypatched to share the test's outer transaction. ~10× cheaper than Phase 1 and avoids locking tables the test never touches.
- **Phase 3** — `pytest-xdist` per-worker DB clones (`_ensure_worker_db`). `pytest -n auto` works because each `gwN` worker gets its own database template.

Two escape hatches:

- **`@pytest.mark.committed_db`** — when a test forks a subprocess CLI (e.g. `ib_sync`, `xenon-ib-place-order`) or builds its own `create_engine()`, that opens a _second physical connection_ that cannot see the outer transaction. Marker switches that test back to Phase 1 TRUNCATE pre+post semantics. Use sparingly — most new tests should leave the marker off.
- **Offline dev** — when Postgres isn't reachable, the autouse fixture silently no-ops. Tests that genuinely require PG depend on the `pg_test_engine` fixture, which skips explicitly when offline.

`DATABASE_URL` and `DATABASE_URL_TEST` are both rewritten to the per-worker DB so subprocess CLIs and helpers that build their own engine from those env vars land on the same database as the parent test.

## CI / Release

`.github/workflows/`:

- `ci.yml` — PR + master push. Runs `web-typecheck`, `web-lint`, `web-tests` (full Vitest against real `.venv` CLIs), `python-tests` (affected on PR, full on master), `version-sync` (VERSION ↔ `package.json`).
- `release.yml` — triggered on tag `v*`. Full verify (pytest + vitest + typecheck + lint) → publish GitHub Release from `CHANGELOG.md`.
- `nightly.yml` — 9 AM UTC Playwright E2E, auto-comments failures on a tracking issue.

**Release cut** (operator-run, does NOT push):

```bash
./scripts/release/cut.sh            # interactive: patch/minor/major/custom
                                    # rewrites VERSION + package.json + CHANGELOG, commits, tags
git push origin master --follow-tags   # operator pushes manually → release.yml fires
```

`VERSION` (root) is the source of truth; `scripts/release/version_sync_check.py` enforces parity with `package.json` in CI.
