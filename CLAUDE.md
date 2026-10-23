# XENON — CLAUDE.md

Master policy file. Topic-specific guidance lives in subdirectory `CLAUDE.md` files:

| Area                                             | File                      |
| ------------------------------------------------ | ------------------------- |
| Frontend, pricing, P&L                           | `web/CLAUDE.md`           |
| Python execution, reports, IB/Futu CLIs          | `src/xenon/CLAUDE.md`     |
| FastAPI, auth, IB Gateway, order lifecycle       | `src/xenon/api/CLAUDE.md` |
| Brand tokens, typography, spectrum, UI rules     | `brand/CLAUDE.md`         |

## Directory layout

| Path              | Purpose                                                | Gitignored?                               |
| ----------------- | ------------------------------------------------------ | ----------------------------------------- |
| `src/xenon/`      | Python package — API, execution, sync, models          | no                                        |
| `web/`            | Next.js frontend                                       | no (`.next/`, `node_modules/` yes)        |
| `lib/tools/`      | Shared TypeScript script runner (`runner.ts`)          | no                                        |
| `scripts/`        | Infra, release, CI helpers                             | no                                        |
| `docs/`           | Architecture, runbooks, research, references           | no                                        |
| `context/memory/` | Agent memory store (episodic + fact)                   | partially (see `context/.gitignore`)      |
| `data/`           | Runtime data files (JSON payloads, caches)             | yes                                       |
| `logs/`           | Runtime logs                                           | yes (`logs/*`, keep `.gitkeep`)           |
| `tmp/`            | Scratch / temp files                                   | yes                                       |
| `output/`         | Tool output, test run artifacts                        | yes                                       |
| `reports/`        | HTML / JSON reports                                    | yes (`*.html`, `*.json`, `.gitkeep` kept) |
| `test-results/`   | Playwright / Vitest test result artifacts              | yes                                       |

### File placement rules

- **Verification screenshots** (browser, Playwright, chrome-devtools MCP) → `output/playwright/<feature>-<date>.png`. Never drop them in the project root. Root-level `*.png` and `docs/plans/*.png` are gitignored.
- **Deploy notes** → gitignored; use `CHANGELOG.md` and the release cut script instead.
- **Plan documents** → `docs/superpowers/plans/YYYY-MM-DD-<slug>.md`; completed plans move to `docs/superpowers/plans/_archive/`.
- **Spec documents** → `docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md`.
- **Logs and temp files** → `logs/` or `tmp/` (both gitignored); never in root.
- **One-time scripts** → `scripts/` with a clear name; delete after use.

## Brokers

- **IB** (primary) — quotes, chains, execution, portfolio. Never bypassed.
- **Futu** (read-only) — positions snapshot via local Futu OpenD. Surfaces as a separate account tab. No order execution and no quote feed; it reads positions and historical deals (`xenon-futu-history-sync`). Silent degrade when OpenD unreachable.

Every execution/portfolio row carries `broker`, `account_env`, `broker_account` columns so paper and live data never blend in a shared Postgres. Resolve scope via `AccountScope` (`src/xenon/execution/account_scope.py`); FastAPI depends on `get_account_scope`, sync subprocesses read `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`. Full policy: `docs/architecture/production-database-strategy.md` § Broker Account Scope.

## Portfolio Structure Classification

- Preserve recognized multi-leg structures: verticals, straddles, synthetics, covered calls, risk reversals, butterflies/condors where classified.
- Do not collapse unrelated two-leg same-symbol/same-expiry singles into `Combo (2 legs)` when structure detection falls through. Split that narrow fall-through back into concrete single-leg rows (`Short Put`, `Long Call`, `Stock`, etc.) so row-level order actions stay available.
- Keep 3+ leg fall-through combos intact unless there is explicit order lineage or a recognized structure; splitting unknown complex orders can misrepresent user intent.
- IB position snapshots do not carry originating combo order id. If exact lineage is needed, join against order submission records instead of guessing from symbol/expiry alone.

## Data Sources

1. **Interactive Brokers** — real-time quotes, chains, execution, live portfolio. Primary.
2. **Futu OpenD** — read-only positions snapshot for the Futu account tab.
3. **Unusual Whales (`$UW_TOKEN`)** — dark-pool flow for `xenon-portfolio-report`, analyst ratings for the Ratings tab (FastAPI `GET /ticker/ratings`, `src/xenon/api/routes/ratings.py`), and ticker info / news / seasonality / previous-close routes in Next.js. Not used for signal generation (removed in the pure-portfolio pivot).
4. **IB Flex Query** — historical fills audit overlay (`xenon-blotter-history`).

**Never use Yahoo Finance.**

## Runtime Data Read Paths

Postgres is the runtime source of truth for portfolio, orders, fills, and journal surfaces. Do not reintroduce silent file fallbacks.

- Portfolio UI reads `xenon.account_snapshots.payload` through FastAPI `GET /portfolio`, scoped by `AccountScope` (`XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`). `data/portfolio.json` is legacy/backfill input only, not a UI read path.
- Orders, fills, journal, attribution surfaces read from `xenon.order_submissions`, `xenon.order_fills`, `xenon.trades`, `xenon.journal_entries` via FastAPI routes.
- Next.js API routes proxy these surfaces via `xenonFetch()`; no `readFile`, `readDataFile`, or cached JSON fallback on runtime paths.

## Dev/Prod DB Split

Two databases on the macmini Postgres. **Everything writes to Postgres first; the website only reads.** Policy: `docs/architecture/production-database-strategy.md` § Dev/Prod DB Split. Procedure and status of the role split: `docs/runbooks/dev-prod-db-cutover.md`.

Roles (state of 2026-10-21):

- `xenon_app` owns both DBs and the schemas `xenon` and `events`. Prod (api, realtime, migrator) writes `core_dev` as `xenon_app`.
- `xenon_dev` exists: LOGIN, no CREATEDB, **read-only on `core_dev`** (SELECT only; INSERT, DELETE and DDL are permission denied). It becomes owner of `core_test` after runbook step 3 (pending, drops data). Until then laptops still use `xenon_app` for `core_test`, and a laptop `xenon_app` URL can write prod.

| DB          | Written by                                                                | Refresh                                                                                                                                                                              |
| ----------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `core_dev`  | macmini Docker stack as `xenon_app` (api, migrator, realtime services)   | source of truth                                                                                                                                                                      |
| `core_test` | MacBook dev sessions via `scripts/infra/dev.sh paper` (`xenon_app` now; `xenon_dev` after step 3) | `pg_dump -n xenon -n events core_dev → pg_restore core_test` at 04:00 ET as `xenon_dev` via `scripts/infra/refresh-core-test.sh`; the LaunchAgent is not installed on the mini yet |

Defense in depth for dev sessions:

1. **Role grants (`xenon_dev` sessions only)** — `xenon_dev` cannot write `core_dev`. Applies only to a session that connects as `xenon_dev`; a `xenon_app` URL bypasses it (runbook step 5/6).
2. **`dev.sh` startup guard** — refuses to boot when `DATABASE_URL` parses to `core_dev`. Test: `scripts/tests/test_dev_sh_db_guard.py`.
3. **Test-DB guard** — root `conftest.py` aborts pytest when the test DB is `core_dev`, matches `DATABASE_URL`, or lacks `test` in its name. Test: `scripts/tests/test_test_db_guard.py`.
4. **`XENON_READ_ONLY=1`** (`dev.sh live`) — refuses every write surface (see below).
5. **CI write-side guard** — `scripts/checks/no_json_write_on_order_path.py` fails new `writeFile` / `Path(...).write_text` / `_atomic_save` against `data/*.json` in `web/app/api/`, `web/lib/order/`, `src/xenon/api/`, `src/xenon/execution/`. Mirror of the existing read-side guard; locks in DB-first as a CI invariant.

### `dev.sh live` is read-only

`scripts/infra/dev.sh live` exports `XENON_READ_ONLY=1`. The flag forces a hard refusal on every write surface so a dev session against live IB never pollutes `core_test` with live fills:

- `POST /orders/{place,cancel,modify}` and `POST /journal` return 403 with top-level `reason_code: READ_ONLY_MODE` (see `src/xenon/api/guards.py::read_only_403`; the toast helper reads `body.reason_code`, not `body.detail.reason_code`).
- `ib_sync._save_portfolio_to_postgres` and `_append_nav_snapshot` are no-ops.
- FastAPI lifespan skips `_run_rehydrate_on_boot`, `_run_fills_replay_on_boot`, and `_maybe_start_activity_poller`.

Real live trading must go through the macmini Docker stack, which writes `core_dev`. Tests: `src/xenon/api/tests/test_read_only_mode.py`, `scripts/tests/test_ib_sync_read_only.py`.

## Order-Path Guards (Layers 1+2)

**Read first:** `docs/reference/order-path-incident-history.md` — chronological log of every non-trivial order-path bug, root cause, fix, and regression test. Append a row when you ship a similar fix.

Automated guards lock in regression patterns. The first two came from PR #61; the write-side guard shipped with the dev/prod DB split. Full design: `docs/superpowers/plans/_archive/2026-04-28-order-path-regression-prevention.md`.

- **Edit-time reminder** (`.claude/hooks/order-path-reminder.sh`) — PreToolUse hook prints an order-path checklist when Claude edits files under `src/xenon/execution/`, `src/xenon/api/server.py`, `src/xenon/api/services/order_entry.py`, `src/xenon/api/routes/order_entry.py`, `src/xenon/api/routes/portfolio.py`, `src/xenon/api/routes/orders*`, `web/app/api/orders/`, `web/lib/order/`, `web/lib/nakedShortGuard.ts`, `web/lib/placeOrderBodySchema.ts`, or `web/lib/orderReasonCodes.ts`. Advisory, never blocks.
- **CI guards** (`.github/workflows/ci.yml::order-path-guards`):
  - `scripts/checks/no_json_fallback_on_order_path.py` — fails new `readDataFile`/`json.load` against `data/*.json` in `web/app/api/` or `src/xenon/api/`. `_ALLOWLIST` is empty; a new entry is a regression, so fix the read.
  - `scripts/checks/no_json_write_on_order_path.py` — write-side mirror. Fails new `writeFile` / `Path(...).write_text` / `_atomic_save` against `data/*.json` in `web/app/api/`, `web/lib/order/`, `src/xenon/api/`, `src/xenon/execution/`. Enforces the DB-first principle from § Dev/Prod DB Split.
  - `scripts/checks/order_path_caller_allowlist.py` — fails imports of `xenon.execution.ib_place_order` / `ib_order_manage` outside `_ALLOWLIST` (the two modules, `src/xenon/api/services/order_entry.py`, the guard itself; test files are exempt), and any mention of the removed CLIs `xenon-ib-place-order` / `xenon-ib-order-manage`. It also protects the future Futu adapter `xenon.execution.futu_orders` (importable only from `services/order_entry.py`) and flags Futu SDK mutations (`place_order`, `modify_order`, `cancel_all_order`, `unlock_trade`) on a Futu trade context anywhere outside that adapter. Locks down [In-process route bypass].
  - The same job also runs `no_ib_insync.py`, `no_pg_insert_nav_history.py` and `no_yahoo_finance.py`.
- **Local pre-commit** (optional): `scripts/checks/install-pre-commit.sh` drops a managed `.git/hooks/pre-commit` that runs all six guards.

To audit the current allowlists: `uv run python scripts/checks/no_json_fallback_on_order_path.py --show-allowlist` (the write-side and caller guards take the same flag).

## Combo / BAG Order Guardrails

1. **Never map combo `Order.action` from debit vs credit.** Combo leg actions define the intended structure; a `SELL` BAG envelope reverses the legs.
2. For entry / open chain combos, keep the envelope on `BUY` and preserve per-leg actions.
3. **Reset stale manual net price** when combo structure changes (single → combo transitions invalidate the previous manual limit price).
4. **Required regressions for combo-entry bugs:** unit test for action / ratio / net-price semantics AND a browser test for displayed combo net price and submitted payload.
5. **Trace the full path before fixing:** chain builder → `/api/orders/place` → FastAPI bridge → `src/xenon/execution/ib_place_order.py`. Identify whether the bug is UI state, payload semantics, or IB combo behavior before patching.

## Cancel / Modify Semantics

Read `src/xenon/api/CLAUDE.md` (§ Order owner, § Cancel / Modify Semantics) before touching cancel / modify code. Critical rules:

1. Place, cancel and modify run in-process on the API pool `orders` connection (clientId `base + 4`), the single order owner (`src/xenon/api/order_owner.py`). No subprocess, no second IB connection, no reconnect as another clientId. IB scopes cancel and modify by clientId, so an order the owner did not place gets 409 `ORDER_NOT_OWNED` with no broker work.
2. Do not trust the original `Trade` object as the only confirmation source.
3. Order disappearance from refreshed open orders counts as cancel success.
4. Preserve upstream status codes and human-readable error detail end-to-end.
5. Route + browser regressions are required, not just Python tests.

## Client IDs

Each environment owns a clientId block, `XENON_IB_CLIENT_ID_BASE` (0-800, step 100; prod `0`, `dev.sh` `100`). Inside the block: pool `sync` / `orders` / `data` = base+3 / 4 / 5, realtime relay base+10..19, `client_id="auto"` base+20..49. On-demand read/data IB scripts must use `client_id="auto"` (no order writes there). Never hardcode an id. Contract: `deploy/README.md` § IB clientId blocks and `docs/runbooks/pr10-order-owner-cutover.md`.

## Frontend Calculation Invariants

Preserve these in all UI pricing / P&L work (full table + tests in `web/CLAUDE.md`):

- Credit / debit sign convention: credits stay negative, debits stay positive. Never `Math.abs()` away sign.
- Spread mid is the sum of per-leg mids.
- Combo natural bid / ask uses cross-fields, not `sign * bid` / `sign * ask`.
- BAG `ComboLeg.action` encodes structure, not trade direction. Always `LONG → BUY`, `SHORT → SELL` regardless of order direction.
- Daily Change % uses `Daily P&L / |Yesterday's Close Value|`, never entry cost.
- Never show underlying price where the user expects option or spread price — show `---` if unavailable.

## Frontend / FastAPI Boundary

- Next.js routes call FastAPI via `xenonFetch()` in `web/lib/xenonApi.ts`. Never `spawn()` from Next.js routes as a fallback path.
- WebSocket auth uses 30s single-use tickets.

## ⛔ Mandatory Rules

1. **Be concise.** No preamble, no filler.
2. **E2E browser verification for ALL UI work.** Primary: `chrome-cdp`. Fallback: Playwright (`web/playwright.config.ts`). No UI change done until visually confirmed. Don't assume code changes produce the expected visual result — verify rendered output in the browser before committing.
3. **Red/green TDD for ALL code.** Failing test → fix → green → refactor. Unit: Vitest, E2E: chrome-cdp/Playwright.
4. **95% coverage target on the touched surface** when practical. Every change includes corresponding tests.
5. **API keys** in `.env` files (see Credentials below). Fallback: `~/.zshrc`.
6. **Options structure reference:** `docs/trading/options-structures.json` + `docs/trading/options-structures.md` — 58 structures, guard decisions, P&L attribution labels. Use for order entry, structure classification, and naked short guard logic.
7. **Todo capture.** When the user says "todo" (e.g. "todo: explore X", "add this as a todo"), append the idea to the **Inbox** section at the bottom of `docs/todo-backlog.md` with today's date. Do not start work on it. Do not silently drop it. The backlog is a queue for future planning sessions, not active work. **Add your own commentary** under each entry — hypotheses, suspected file sites, dependencies on other backlog items, design questions, references to commits/CLAUDE.md guidance — anything that would save future-you from rebuilding the context from scratch. Use a `**Notes:**` sub-bullet to keep your commentary visually separate from the user's original framing.

## Identity

**Xenon** — broker terminal for options portfolio management. Live IB + read-only Futu integration. Places orders, tracks fills, enforces the naked-short guard, surfaces P&L and Greeks attribution. **No signal generation, no scanner output — bring your own thesis.** The signal layer (scanners, fetchers, analysis, share-card generators) was removed in the pure-portfolio pivot (commit `cc568c36`); do not reintroduce scanner / evaluation / discovery surfaces.

Brand spec: `brand/CLAUDE.md` + `docs/reference/brand-identity.md`. Non-negotiables for any UI/asset work: `signal.core` teal is the flagship accent; all colors via design tokens — no raw hex; Inter for UI, IBM Plex Mono for numeric/telemetry, Sohne for display only; panels max out at 4px radius (badges may use capsule); no glassmorphism, decorative gradients, or soft shadows; empty states describe measurement conditions; voice is precise, calm, scientific.

## ⛔ Naked-Short Guard — Mandatory, No Exceptions

The system must never allow naked short exposure. Every short call must be covered by long shares (1 contract = 100 shares) or by long calls at the same expiry. Cash-secured puts are allowed. Combos that net to uncovered short calls are blocked at two layers: UI pre-submission (`web/lib/nakedShortGuard.ts`) and API gate (`/api/orders/place` returns 403). A third layer, the post-sync audit (`naked_short_audit.py`), runs after every `ib_sync` and logs violations and records `service_health`. By default (shadow mode) it cancels nothing. Enforce mode is opt-in: `XENON_AUDIT_CANCEL=1` on the API process (default off) makes the API cancel eligible violating orders, and only orders the order owner placed (`src/xenon/api/services/audit_enforce.py`). Exposure from TWS / mobile / other-client orders is reported, never cancelled.

Full enforcement matrix and combo logic: `src/xenon/CLAUDE.md` § Naked Short Protection.

## Credentials

| File          | Loader                                             | Contains                                                                                                                                                                                                                                                  |
| ------------- | -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.env` (root) | `python-dotenv`; `dev.sh`; prod `env_file`         | `DATABASE_URL`, `DATABASE_URL_TEST`, `XENON_INTERNAL_API_TOKEN` (required: Next→FastAPI auth), `XENON_QUERY_API_KEY` / `MDW_API_KEY` (optional read-only keys), `IB_FLEX_TOKEN` / `IB_FLEX_QUERY_ID` / `IB_FLEX_NAV_QUERY_ID` (optional)                      |
| `web/.env`    | Next.js; also loaded by `api/server.py` in dev     | `ANTHROPIC_API_KEY` (chat), `UW_TOKEN` (Unusual Whales), `EXA_API_KEY` (optional ticker-page enrichment)                                                                                                                                                   |

`IB_FLEX_TOKEN` + `IB_FLEX_QUERY_ID` are optional. When unset, the Historical Trades panel renders an empty state with a setup hint instead of erroring; `xenon-blotter-history` and `POST /blotter` exit gracefully with `configured=false`. Set both to enable IB Flex Query as an audit overlay (see `uv run python -m xenon.trade_blotter.flex_query --setup`).

`UW_TOKEN` is read by Next.js routes (`web/.env`) and by FastAPI (`UWClient`, `xenon-portfolio-report`). `api/server.py` loads both `.env` files in dev. Prod: the api and web containers both load `/opt/xenon/web.env` (`deploy/compose.prod.yml`), so the one file feeds both.

**Auth model.** There is no Clerk and no user login. Prod is tailnet-only. `web/middleware.ts` guards `/api/**` against cross-site browser requests (Host allowlist `XENON_ALLOWED_HOSTS`, fetch-metadata and Origin checks). FastAPI is fail-closed and credential-only (`src/xenon/api/auth.py`): `X-Internal-Token` = `XENON_INTERNAL_API_TOKEN` (Next→API, full access), or a path-scoped `X-API-Key` (`XENON_QUERY_API_KEY`, `MDW_API_KEY`), else 401. `/health` is the only exempt path. `XENON_AUTH_ALLOW_DEV_OPEN=1` opens the API for dev debugging only; `dev.sh` does not set it and prod never does. Details: `src/xenon/api/CLAUDE.md` § Auth.

## Market Hours

```bash
uv run xenon-market-hours   # market-hours helper (xenon.utils.market_calendar)
```

`xenon.utils.market_calendar` is the only Python market-open check: regular session 09:30–16:00 ET, Mon–Fri, ET-aware (a naive `datetime` raises), full-day holidays from `src/xenon/config/market_holidays.json`. Early closes (13:00 ET) are not modelled. The web `useMarketHours` hook and `scripts/infra/ib_realtime/ib_realtime_server.js` keep their own checks.

Quotes, fills, and position updates flow whenever IB Gateway is connected. Outside RTH the portfolio still renders from the last `account_snapshots.payload`; flag stale data in the UI rather than swap to a different source.

## Startup Checklist

`scripts/infra/dev.sh paper|live` derives the IB Gateway port (4001 live / 4002 paper), exports `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`, runs Alembic migrations, and starts FastAPI + Next dev. It does **not** edit `.env` and does **not** start IB Gateway (manual). It refuses to start without `XENON_INTERNAL_API_TOKEN` (env or `.env`); the only argument is the mode.

- [ ] `scripts/infra/dev.sh paper` (paper IB on local `127.0.0.1:4002`, writes `core_test`) — OR `scripts/infra/dev.sh live` (live IB on macmini `100.66.147.98:4001`, **read-only — `XENON_READ_ONLY=1`**)
- [ ] Dev stack binds **3200** (Next), **8421** (FastAPI), **8866** (realtime WS). Prod is the Docker stack on the macmini (`deploy/compose.prod.yml`, published on 127.0.0.1:3000 / 8321 / 8765); the dev ports are offset so the xenon dev stack coexists with the local radon stack.
- [ ] If cold start: approve 2FA on IBKR mobile
- [ ] `psql -h 100.66.147.98 -U xenon_app core_test -c "SELECT 1"` — verify Postgres reachable (`xenon_app` until runbook step 3 gives `xenon_dev` ownership of `core_test`; `-U xenon_dev core_dev` is the read-only prod check). `dev.sh` refuses to start if `DATABASE_URL` points at `core_dev` (prod).
- [ ] `curl http://localhost:8421/health` — verify `ib_gateway.port_listening: true` and `status: ok` (`orders_halted` means the order owner latched; see `src/xenon/api/CLAUDE.md` § Order owner)
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
| Run a CLI entry point | `uv run xenon-portfolio-report ...`                   |
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

**Prod-DB guard.** The root `conftest.py` sets `DATABASE_URL` to the test DB (`DATABASE_URL_TEST`, per-worker under xdist) before collection. Import-time singletons, the autouse fixtures, and subprocess CLIs that inherit the env then use the test DB, not the exported/`.env` `DATABASE_URL`. A child process that gets an explicitly replaced env is outside this guard. The session exits with an error when the test DB has the same host+dbname as `DATABASE_URL`, is `core_dev`, or has no `test` in its name, when either URL has a query key outside the allowlist (`sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `connect_timeout`, `application_name`; other keys such as `dbname`, `host`, or asyncpg `dsn` can re-route the connection), or when only one of the two URLs has an empty host (socket/`PGHOST` routed; if both are empty, only the dbnames are compared). Fix: export `DATABASE_URL_TEST` to a separate `*_test` DB with a plain URL. Tests: `scripts/tests/test_test_db_guard.py`.

### Pytest infrastructure (3-phase speedup)

The full Python suite is intentionally cheap to run locally. Don't disable or bypass these layers:

- **Phase 1** — session-scoped engine + single `TRUNCATE` reset (`src/xenon/_test_db.py::truncate_all_xenon_tables`).
- **Phase 2** — autouse `BEGIN/ROLLBACK` per test, via `_postgres_orders_test_db` in `scripts/tests/conftest.py`. Each test sees an empty schema; the app engine is monkeypatched to share the test's outer transaction. ~10× cheaper than Phase 1 and avoids locking tables the test never touches.
- **Phase 3** — `pytest-xdist` per-worker DB clones (`_ensure_worker_db`). `pytest -n auto` works because each `gwN` worker gets its own database, cloned from the unsuffixed test DB (only the parsed database name gets the `_gwN` suffix; the query string is kept). A worker DB that cannot be created (no `CREATEDB`, no pre-create) fails the worker loudly; workers never fall back to one shared DB.

Two escape hatches:

- **`@pytest.mark.committed_db`** — when a test forks a subprocess CLI (e.g. `xenon-ib-sync`, `xenon-ib-orders`) or builds its own `create_engine()`, that opens a _second physical connection_ that cannot see the outer transaction. Marker switches that test back to Phase 1 TRUNCATE pre+post semantics. Use sparingly — most new tests should leave the marker off.
- **Offline dev** — when Postgres isn't reachable, the autouse fixture silently no-ops (it still clears the cached app engines). Only connection errors count as offline, in the initial reachability probe and in TRUNCATE (psycopg engines: SQLSTATE 08/57P0x, connect failures without SQLSTATE, or an InterfaceError that names a closed/lost connection); any other error (missing schema, deadlock, permission denied, a closed cursor) fails loudly. Tests that genuinely require PG depend on the `pg_test_engine` fixture, which skips explicitly when offline.

`DATABASE_URL` and `DATABASE_URL_TEST` are both rewritten to the per-worker DB so subprocess CLIs and helpers that build their own engine from those env vars land on the same database as the parent test.

## CI / Release

`.github/workflows/`:

- `ci.yml` — PR + master push. Jobs: `web-typecheck`, `web-lint`, `web-tests` (full Vitest against real `.venv` CLIs), `python-tests` (affected on PR, full on master; needs Postgres), `version-sync` (VERSION ↔ `package.json`), `ruff-pyflakes` (`ruff check --select F src scripts`), `order-path-guards` (the six `scripts/checks` guards), `relay-tests` (`node:test` suite for the IB realtime relay).
- `release.yml` — triggered on tag `v*`. Jobs: `verify` (pytest + vitest + typecheck + lint + version sync) → `publish` (GitHub Release from `CHANGELOG.md`) and `ghcr-push` (builds and pushes the api / web / realtime / migrator images to GHCR as `:X.Y.Z`) → `promote-latest` (moves `:latest`; skipped for prerelease tags). There is no nightly workflow.

**Release cut** (never push `master`):

1. `./scripts/release/cut.sh` from a clean, synced `master` (interactive: patch / minor / major / custom). It rewrites `VERSION`, `package.json`, `web/package.json` and `CHANGELOG.md`, commits `release: vX.Y.Z` and creates a local tag. Ignore any hint to push `master`.
2. Move the release commit to a branch: `git switch -c release/vX.Y.Z`, `git tag -d vX.Y.Z`, `git branch -f master origin/master`. Push the branch and open a PR.
3. After CI is green, merge the PR.
4. **Pause Watchtower on the mini and confirm it is stopped** before any tag push (`docker stop xenon-watchtower-1`, then `docker inspect -f '{{.State.Running}}' xenon-watchtower-1` must print `false`; command in `docs/runbooks/remote-deploy.md` § Standard release flow, step 2). The tag push moves `:latest`, and a running Watchtower recreates the api on its own, with no socket check.
5. Tag the merge commit: `git fetch origin && git tag -a vX.Y.Z -m vX.Y.Z $(gh pr view <PR> --json mergeCommit --jq .mergeCommit.oid)`.
6. Push the tag alone: `git push origin refs/tags/vX.Y.Z`. `release.yml` fires.
7. Deploy is the tag push building the images, then the operator cutover on the mini: `docs/runbooks/remote-deploy.md` § Standard release flow, steps 4-9. A merge alone never deploys.

`VERSION` (root) is the source of truth; `scripts/release/version_sync_check.py` enforces parity with `package.json` in CI.

## Strategy Registry Sync

`docs/trading/strategies.md` is the source of truth. `data/strategies.json` is gitignored local data (read by `src/xenon/reports/portfolio_attribution.py`). If `strategies.md` changes, update your local `data/strategies.json` and validate it with `uv run python -m json.tool data/strategies.json`. The signal layer that consumed the registry was removed in the pure-portfolio pivot.
