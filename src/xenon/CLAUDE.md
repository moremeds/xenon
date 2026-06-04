# src/xenon/ — CLAUDE.md

Python execution, broker integration, portfolio reports — the installable `xenon` package. Root `CLAUDE.md` is authoritative for policy; `src/xenon/api/CLAUDE.md` covers FastAPI/IB Gateway infra.

## Data Sources

1. **Interactive Brokers (TWS/Gateway)** — real-time quotes, chains, execution, live portfolio. Primary.
2. **Unusual Whales (`$UW_TOKEN`)** — historic stock OHLC + option contract history for portfolio reports (`xenon-portfolio-perf`, `xenon-portfolio-report`). Data-source role only — signal generation was removed in the pure-portfolio pivot.
3. **Futu OpenD** — read-only positions snapshot.
4. **IB Flex Query** — historical fills audit overlay.

**Never use Yahoo Finance.**

**Clients:** `src/xenon/clients/` — `IBClient`, `UWClient`, `FutuClient`.

**Futu (read-only):** `src/xenon/clients/futu_client.py` — positions snapshot from local Futu OpenD. Never write, never subscribe to market data. Server-side singleton with asyncio singleflight lock in `src/xenon/api/server.py` (`/futu/sync` / `/futu/portfolio`). Silent degrade when OpenD unreachable.

## UW API Observability

`src/xenon/utils/uw_api_stats.py` — thread-safe singleton that records every `UWClient._get()` call (latency, cache hits, retries, rate-limits, errors). Backed by the `xenon.uw_api_stats` Postgres table. UWClient is used in subprocess by portfolio-report CLIs; the stats persist across runs.

`src/xenon/utils/uw_cache.py` is lock-protected because `UWClient._get` runs under `asyncio.to_thread()` and multiple evaluator threads hit the cache concurrently — **do not drop the lock**.

## Database

Postgres is the primary persistence layer. `src/xenon/db/` owns:

- `engine.py` — async SQLAlchemy engine for FastAPI plus sync psycopg engine for subprocess callers
- `schema.py` — SQLAlchemy Core table definitions for `xenon.*` and `events.*`
- `queries/` — focused query modules for portfolio, orders, trades, wizard, combo wizard
- `events.py` — LISTEN/NOTIFY helpers and outbox consumption
- `migrations/` — Alembic environment and migration versions

Add a table by updating `schema.py`, then run:

```bash
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head
```

Add or update the matching `src/xenon/db/queries/` module in the same change. Use `get_engine()` in async FastAPI contexts after `init_engine()` has run. Use `get_sync_engine()` for subprocess/synchronous callers such as `ib_sync`, `ib_execute`, `orders_store`, and combo wizard code.

Database events use `events.py` plus the Postgres outbox trigger. Emit durable events by writing outbox rows; subscribe with the LISTEN/NOTIFY helpers for reactive services.

### Dev/Prod DB Split

Two DBs on the macmini Postgres: `core_dev` (prod, written exclusively by the macmini Docker stack via the `xenon_prod` role) and `core_test` (dev mirror, written from MacBook sessions via `xenon_dev`). One-way nightly refresh `core_dev → core_test` at 04:00 ET (`scripts/infra/refresh-core-test.sh`). `dev.sh` refuses to boot when `DATABASE_URL` parses to `core_dev`. Adding a new migration: run `uv run alembic upgrade head` against your dev DB only — the macmini Docker `migrator` service applies it to `core_dev` on next deploy. Full policy: root `CLAUDE.md` § Dev/Prod DB Split.

### Writer CLIs respect `XENON_READ_ONLY=1`

`ib_sync._save_portfolio_to_postgres` and `_append_nav_snapshot` no-op when `XENON_READ_ONLY=1` is set (exported by `dev.sh live` for debugging-against-live-IB sessions). Naked-short audit, fills replay, and similar writer CLIs called from the FastAPI lifespan are skipped at boot under the same flag. New persistence code in this package must check the flag — see `src/xenon/api/CLAUDE.md` § Read-Only Mode for the full surface map.

### Broker Account Scope

All execution and portfolio tables carry `broker`, `account_env`, `broker_account` columns so paper/live data never blends in a shared Postgres. Full policy: `docs/architecture/production-database-strategy.md`.

Key rules:

- Every write must include scope — never rely on `server_default` for new rows.
- Every query in an active workflow (rehydrate, monitor, working-orders) must filter by scope.
- `legacy_unknown` rows are excluded from active flows when scope filters are active.
- Order idempotency key is `(broker, account_env, broker_account, user_id, client_attempt_id)`.
- `nav_history` PK is `(broker, account_env, broker_account, date, source)` — `source` joined the PK in migration `2026_06_03_nav_src_pk` so intraday and close NAV rows for the same scope+date coexist as audit rows.
- Use `AccountScope` from `src/xenon/execution/account_scope.py` — never hardcode scope values in query code.
- FastAPI: depend on `xenon.api.guards.get_account_scope`.
- Sync subprocesses: env vars `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`.

## Combo / BAG Order Guardrails

1. **Never map combo `Order.action` from debit vs credit.**
   - In IB, combo leg actions define the intended structure.
   - A `SELL` BAG envelope reverses the legs.
   - For entry/open chain combos, keep the envelope on `BUY` and preserve per-leg actions.
2. **When the order-builder structure changes, clear stale top-level manual net pricing.**
   - Single-leg → combo transitions must invalidate the previous manual limit price.
   - Recompute the limit field from the normalized combo quote for the current structure.
3. **Required regressions for combo-entry bugs:**
   - unit test for combo action/ratio/net-price semantics
   - browser test for displayed combo net price and submitted payload
4. **Trace the full path before fixing:**
   - chain builder → `/api/orders/place` → FastAPI bridge → `src/xenon/execution/ib_place_order.py`
   - verify whether the bug is UI state, payload semantics, or IB combo behavior before patching

## Naked Short Protection (Gate 4)

**Hard rule — no exceptions.** The system must never allow naked short exposure.

| Scenario                                          | Rule                                               | Action                      |
| ------------------------------------------------- | -------------------------------------------------- | --------------------------- |
| SELL stock, no long shares                        | Naked short stock                                  | BLOCK                       |
| SELL call, no long shares or long calls           | Naked short call                                   | BLOCK                       |
| SELL call, long calls at same expiry (any strike) | Vertical spread                                    | ALLOW                       |
| SELL N call contracts, shares < N × 100           | Short a tail                                       | BLOCK                       |
| SELL put (cash-secured)                           | Defined risk                                       | ALLOW                       |
| Vertical spread (BUY C + SELL C)                  | Long call covers short                             | ALLOW                       |
| Short risk reversal (SELL C + BUY P)              | Naked short call — long put does not cover         | BLOCK                       |
| 1x2 ratio spread (BUY 1C + SELL 2C)               | 1 uncovered short call                             | BLOCK (unless stock covers) |
| Jade Lizard / Seagull (BUY C + SELL C + SELL P)   | Call spread covers short call; put is cash-secured | ALLOW                       |
| Combo closing (action=SELL)                       | Reduces exposure                                   | ALLOW                       |
| BUY anything                                      | No short exposure                                  | ALLOW                       |

**Enforcement layers:**

1. **UI pre-submission** — `checkNakedShortRisk()` in `OrderTab.tsx` blocks form submission
2. **API gate** — `orders/place/route.ts` returns 403 if guard fails
3. **Post-sync audit** — `naked_short_audit.py` runs after every `ib_sync`, cancels violating open orders

**Combo check design**: IB BAG orders always use `action=BUY` envelope. Guard inspects leg-level `right` and `action` fields. `sellCallRatio - buyCallRatio` = uncovered short calls. Checked before the BUY early-return.

**Implementation**: `web/lib/nakedShortGuard.ts` (shared guard), `src/xenon/execution/naked_short_audit.py` (audit + cancel)
**Tests**: `web/tests/naked-short-guard.test.ts`, `scripts/tests/test_naked_short_audit.py`

## Order Execution Modules

`src/xenon/execution/` — all IB order/fill logic:

- `ib_place_order.py`, `ib_order_manage.py` (cancel/modify via subprocess; see `api/CLAUDE.md`)
- `orders_store.py` — Postgres-backed public facade for `order_submissions` and `order_events`; preserve its function signatures
- `single_leg_rehydrate.py` — three-source reconcile (orders DB, IB open orders, position monitor). Invoked in FastAPI lifespan on boot; exposed for testing via `POST /dev/rehydrate/synthetic`.
- `naked_short_audit.py` — post-sync enforcement
- `quote_guard.py`, `contract_normalize.py`, `preflight.py` — order-path safety layers
- `combo_wizard/` — combo order entry flow (plan/submit/reprice/abort/protect)
- `futu_sync.py` — read-only Futu positions sync

**Note:** `quote_tokens.py` exists but the integration flow (#34) was reverted (#35) — do not wire it back into `/orders/quote` without re-reviewing.

## Dev Environment

Python deps via **`uv`** (not pip). `pyproject.toml` defines `[project.optional-dependencies].test` (pytest + pytest-asyncio). Locally:

```bash
uv sync --extra test                  # install deps incl. test
uv run xenon-portfolio-perf --json    # run any CLI entry point
uv run pytest -xvs <path>             # single test
```

CI uses `uv sync --frozen --extra test` then `uv run pytest` — affected-on-PR, full-on-master.

## Legacy data files

Postgres is primary for portfolio, orders, NAV, trades, UW stats, and journal entries. Files under `data/` are backup or cache inputs unless a module explicitly documents otherwise.

## Commands

| Command                   | Action                                                             |
| ------------------------- | ------------------------------------------------------------------ |
| `xenon-api`               | FastAPI server (`localhost:8321`) — used by `dev.sh`               |
| `xenon-ib-sync`           | Pull live portfolio + open orders from IB into Postgres            |
| `xenon-ib-place-order`    | Subprocess placeholder for the place-order path                    |
| `xenon-ib-order-manage`   | Subprocess for cancel/modify (uses original clientId)              |
| `xenon-ib-orders`         | Inspect IB open orders                                             |
| `xenon-ib-option-chain`   | Fetch options chain for a symbol (used by `/options/chain`)        |
| `xenon-ib-reconcile`      | Reconcile IB vs Postgres orders + fills                            |
| `xenon-ib-execute`        | Direct execution helper                                            |
| `xenon-futu-sync`         | Pull read-only positions from local Futu OpenD                     |
| `xenon-naked-short-audit` | Post-sync cancellation of any uncovered short positions            |
| `xenon-blotter`           | Today's fills + P&L                                                |
| `xenon-blotter-history`   | Historical trades (Flex Query)                                     |
| `xenon-portfolio-report`  | HTML portfolio report                                              |
| `xenon-portfolio-attrib`  | P&L attribution (incl. Kelly calibration display fields)           |
| `xenon-portfolio-perf`    | Performance computation (drawdown, return series, OHLC enrichment) |
| `xenon-perf-explainer`    | Performance-page HTML explainer                                    |
| `xenon-monitor-daemon`    | Position-rules + fill monitor + preset rebalance daemon            |
| `xenon-position-rules`    | Position-close rule CLI                                            |
| `xenon-preset-rebalance`  | Preset rebalance handler                                           |
| `xenon-market-hours`      | Market-hours helper                                                |
| `xenon-presets`           | Preset list helper                                                 |
