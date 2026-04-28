# XENON — CLAUDE.md

Master policy file. Topic-specific guidance lives in subdirectory `CLAUDE.md` files:

| Area                                             | File                      |
| ------------------------------------------------ | ------------------------- |
| Frontend, pricing, P&L, share cards, reports     | `web/CLAUDE.md`           |
| Python pipelines, scanners, commands, data files | `src/xenon/CLAUDE.md`     |
| FastAPI, Clerk auth, IB Gateway, order lifecycle | `src/xenon/api/CLAUDE.md` |
| Brand tokens, typography, spectrum, UI rules     | `brand/CLAUDE.md`         |

## Brokers

- **IB** (primary) — quotes, chains, execution, portfolio. Never bypassed.
- **Futu** (read-only) — positions snapshot via local Futu OpenD. Surfaces as a separate account tab. No orders, no fills, no quotes. Silent degrade when OpenD unreachable.

Every execution/portfolio row carries `broker`, `account_env`, `broker_account` columns so paper and live data never blend in a shared Postgres. Resolve scope via `AccountScope` (`src/xenon/execution/account_scope.py`); FastAPI depends on `get_account_scope`, sync subprocesses read `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`. Full policy: `docs/architecture/production-database-strategy.md` § Broker Account Scope.

## Scanner Hierarchy

- `src/xenon/scanners/_shared/` — shared foundation (cache, executor, models, scoring, universe)
- `src/xenon/scanners/trend/` (entry: `xenon-trend-scan`) — **DEPRECATED.** Code retained for repurposing; R2/ta_lib data source removed 2026-04-26. Scheduler removed from server.py.
- `src/xenon/scanners/uw/` (entries: `xenon-uw-scan`, `xenon-uw-analyze`) — tiered UW signal scanner with Type F confluence

New scanners MUST build on `src/xenon/scanners/_shared/` — do not duplicate universe/executor/scoring logic.

## Portfolio Structure Classification

- Preserve recognized multi-leg structures: verticals, straddles, synthetics, covered calls, risk reversals, butterflies/condors where classified.
- Do not collapse unrelated two-leg same-symbol/same-expiry singles into `Combo (2 legs)` when structure detection falls through. Split that narrow fall-through back into concrete single-leg rows (`Short Put`, `Long Call`, `Stock`, etc.) so row-level order actions stay available.
- Keep 3+ leg fall-through combos intact unless there is explicit order lineage or a recognized structure; splitting unknown complex orders can misrepresent user intent.
- IB position snapshots do not carry originating combo order id. If exact lineage is needed, join against order submission records instead of guessing from symbol/expiry alone.

## Data Source Priority

1. Interactive Brokers — real-time quotes, chains, execution, live portfolio
2. Unusual Whales (`$UW_TOKEN`) — dark pool, sweeps, alerts (Stage B/C).
3. Web scrape — last resort.

**Never use Yahoo Finance.**

## Runtime Data Read Paths

Postgres is the runtime source of truth for migrated analytics and portfolio surfaces. Do not reintroduce silent file fallbacks.

- Portfolio UI reads `xenon.account_snapshots.payload` through FastAPI `GET /portfolio`, scoped by `AccountScope` (`XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`). `data/portfolio.json` is legacy/backfill input only, not a UI read path.
- VCG History reads the latest `xenon.vcg_series` row through FastAPI `GET /vcg`. `data/vcg.json` is legacy/backfill input only; `/vcg/scan` cooldown/cache checks also stay in Postgres.
- GEX writes to both `scan_results` and `gex_snapshots`; UW analyze snapshots keep full payload JSONB plus generated columns and fanout child tables.
- Next.js API routes proxy these migrated surfaces via `xenonFetch()`; no `readFile`, `readDataFile`, or cached JSON fallback on runtime paths.
- Preserve CI guard tests that prove stale JSON files are not read (`scripts/tests/test_vcg_json_not_read.py`, portfolio payload/route tests).

## ⛔ Mandatory Rules

1. **Be concise.** No preamble, no filler.
2. **E2E browser verification for ALL UI work.** Primary: `chrome-cdp`. Fallback: Playwright (`web/playwright.config.ts`). No UI change done until visually confirmed. Don't assume code changes produce the expected visual result — verify rendered output in the browser before committing.
3. **Red/green TDD for ALL code.** Failing test → fix → green → refactor. Unit: Vitest, E2E: chrome-cdp/Playwright.
4. **95% test coverage target.** Every change includes corresponding tests.
5. **API keys** in `.env` files (see Credentials below). Fallback: `~/.zshrc`.
6. **Options structure reference:** `docs/trading/options-structures.json` + `docs/trading/options-structures.md` — 58 structures, guard decisions, P&L attribution labels. Use for order entry, structure classification, and naked short guard logic.
7. **Todo capture.** When the user says "todo" (e.g. "todo: explore X", "add this as a todo"), append the idea to the **Inbox** section at the bottom of `docs/todo-backlog.md` with today's date. Do not start work on it. Do not silently drop it. The backlog is a queue for future planning sessions, not active work. **Add your own commentary** under each entry — hypotheses, suspected file sites, dependencies on other backlog items, design questions, references to commits/CLAUDE.md guidance — anything that would save future-you from rebuilding the context from scratch. Use a `**Notes:**` sub-bullet to keep your commentary visually separate from the user's original framing.

## Identity

**Xenon** — market structure reconstruction system. Surfaces convex opportunities from dark pool/OTC flow, vol surfaces, cross-asset positioning. Detects institutional positioning, constructs convex options structures, sizes with fractional Kelly. **Flow signal or nothing.**

Brand spec: `brand/CLAUDE.md` + `docs/reference/brand-identity.md`.

## ⛔ Four Gates — Mandatory, Sequential, No Exceptions

```
GATE 1 — CONVEXITY      : Potential gain ≥ 2× potential loss. Defined-risk only (long options, verticals).
GATE 2 — EDGE           : Specific, data-backed dark pool/OTC signal that hasn't moved price yet.
GATE 3 — RISK MGMT      : Fractional Kelly sizing. Hard cap: 2.5% of bankroll per position.
GATE 4 — NO NAKED SHORTS: Never naked short stock, calls, futures, or bonds. Every short call must be fully covered by long shares (1 contract = 100 shares). Violation = immediate cancel.
```

**Any gate fails → stop. No rationalization.** Enforcement details: `src/xenon/CLAUDE.md` (naked-short table + combo guardrails).

## Credentials

| File          | Loader          | Contains                                                                                                                                                                                                                                                                          |
| ------------- | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.env` (root) | `python-dotenv` | `DATABASE_URL`, `DATABASE_URL_TEST`, `MENTHORQ_USER`, `MENTHORQ_PASS`, `MASSIVE_API_KEY`, `CLERK_JWKS_URL`, `CLERK_ISSUER`, `ALLOWED_USER_IDS`, `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `IB_FLEX_TOKEN` (optional), `IB_FLEX_QUERY_ID` (optional) |
| `web/.env`    | Next.js         | `ANTHROPIC_API_KEY`, `UW_TOKEN`, `EXA_API_KEY`, `CEREBRAS_API_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`                                                                                                                                                       |

`IB_FLEX_TOKEN` + `IB_FLEX_QUERY_ID` are optional. When unset, the Historical Trades panel renders an empty state with a setup hint instead of erroring; `xenon-blotter-history` and `POST /blotter` exit gracefully with `configured=false`. Set both to enable IB Flex Query as an audit overlay (see `uv run python -m xenon.trade_blotter.flex_query --setup`).

## Market Hours

```bash
TZ=America/New_York date +"%A %H:%M"   # 9:30–16:00 ET, Mon–Fri
```

- **Open**: Fetch fresh. Cache TTL: flow 5min, ratings 15min.
- **Closed**: Use latest. Flag stale data.

### UW API budget controls

Daily UW budget: **20,000 calls/day**. The `/uw-analyze` page stays within budget via:

- **30-min snapshot TTL** during open hours (overridable via env)
- **Automatic refresh blocked entirely** outside 9:30–16:00 ET (weekdays)
- **Weekday holidays treated as OPEN** (known gap — neither backend nor frontend has a holiday calendar; cost is ~1 day over-budget per US holiday)
- **Refresh button always works** — `POST /uw-analyze/refresh` and `/uw-analyze?ticker=X` pass `user_initiated=True` which bypasses the closed-market gate

Env vars (read per-call, runtime tunable):

- `XENON_UW_TTL_OPEN_S` (default `1800`) — snapshot TTL during open hours
- `XENON_UW_TTL_CLOSED_S` (default `3600`) — TTL for user-initiated fetches when market is closed

The closed-market gate lives inside `UwAnalyzeCache.get_or_run()` and also covers the separate on-demand OI fetch in `_process_ticker`. See `src/xenon/api/services/uw_analyze_cache.py` + `src/xenon/api/routes/uw_analyze.py` for the implementation.

## Output Rules

- Always: `signal → structure → Kelly math → decision`
- State probabilities; flag uncertainty
- Failing gate = immediate stop, name the gate
- **Never rationalize a bad trade**
- Executed → `trade_log.json` | NO_TRADE → `docs/status.md`

## Startup Checklist

- [ ] `scripts/cloud.sh` (default — local dev services + VPS IB Gateway via Tailscale) — OR `scripts/local.sh` (fully local with Docker gateway)
- [ ] If local mode: approve 2FA on IBKR mobile for cold start
- [ ] `psql -h localhost -U xenon_app xenon_db -c "SELECT 1"` — verify Postgres accessible
- [ ] `curl http://localhost:8321/health` — verify `ib_gateway.port_listening: true`
- [ ] Reconciliation auto-runs → `data/reconciliation.json`
- [ ] Exit order service auto-runs (PENDING_MANUAL)
- [ ] CRI scan service running (30-min intervals)
- [ ] X scan if >12h stale
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
