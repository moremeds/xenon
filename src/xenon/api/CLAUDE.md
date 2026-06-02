# src/xenon/api/ — CLAUDE.md

FastAPI bridge between Next.js and IB Gateway / Futu OpenD. Root `CLAUDE.md` is authoritative for policy. Infrastructure reference (files, ports, gateway modes, auth component map, deployment): `docs/architecture/api-infrastructure.md`.

**Read this before touching the order/cancel/modify/blotter/rehydrate path:** `docs/reference/order-path-incident-history.md` — chronological table of every non-trivial bug in this surface, with root cause, solution, and the regression test that protects against recurrence. Append a row when you ship a similar fix.

## Core rule

Next.js routes call FastAPI (`localhost:8321`) via `xenonFetch()` (`web/lib/xenonApi.ts`). **Never `spawn()`**. No spawn fallback — always try FastAPI first.

## Module Layout

- `server.py` — endpoint dispatch, IB pool lifecycle, portfolio + orders + Futu inline routes
- `routes/` — per-topic FastAPI routers, included from `server.py`:
  - `orders.py` — `/orders/*` (list, refresh, dependencies)
  - `historical.py` — historical bars / fills
  - `journal.py` — trade journal entries
  - `trades.py` — closed trades
  - `position_rules.py` — position-close rule CRUD
  - `wizard.py` — combo order wizard (plan/submit/reprice/abort/protect)
- `services/` — business logic (stateful, testable without HTTP):
  - `advisory_lock.py` — Postgres advisory locks
  - `ib_activity_mirror.py` — IB→PG fills + open-orders mirror
  - `journal_auto_import.py` — PG-event-driven journal auto-import
  - `position_rules_cancel.py`, `position_rules_health.py` — position-rule supervision
- `ib_pool.py` — persistent IB connection pool (clientId 0–9)
- `pool_order_manage.py` — pool-based helpers (but see cancel/modify rule below — real cancel/modify uses subprocess)
- `ws_ticket.py` — 30s single-use WebSocket auth tickets
- `ib_gateway.py` — docker/cloud/launchd gateway lifecycle
- `auth.py` — Clerk JWT + API key dependencies (localhost bypass inside)

**New endpoint?** Add a router module under `routes/`, register it in `server.py`. Business logic goes in `services/`, not inline in the route.

## Background Tasks

- **Futu singleton** — lazy-initialized on first `/futu/sync` call so the server boots even when OpenD is down. asyncio singleflight lock collapses concurrent fetches. 10s cooldown gate. **Uses a `None` sentinel (not `0.0`) for last-sync** — near process start `time.monotonic() - 0.0` would look recently-synced and serve stale cache.
- **Single-leg rehydrate on boot (F7)** — `_run_rehydrate_on_boot()` runs synchronously inside the FastAPI `lifespan` before the server begins serving. Calls `xenon.execution.single_leg_rehydrate.rehydrate_on_boot()` (three-source reconcile: orders DB, IB open orders, CRI monitor) with a 10s timeout; on timeout or failure we log a warning and continue. Skipped in test mode unless `XENON_ORDERS_DB_PATH` is set (prevents polluting the prod DuckDB during pytest). Observability readiness check: `POST /dev/rehydrate/synthetic`.
- **IB→Postgres activity mirror** — `xenon.api.services.ib_activity_mirror`. Two surfaces, one service:
  1. `_run_fills_replay_on_boot()` runs once at boot after rehydrate (30s timeout). Pulls `client.get_fills()` and inserts into `xenon.order_fills` via `record_external_fills`. Catches fills missed during downtime — without it, TWS-placed/modified orders that filled while FastAPI was down stay invisible to the blotter.
  2. `_maybe_start_activity_poller()` starts a forever-loop background task by default; set `XENON_IB_ACTIVITY_POLLER=0` only for special suppression. Cadence env: `XENON_IB_ACTIVITY_POLL_S` (default 60). Each tick runs `sync_open_orders_to_postgres` + `record_external_fills` independently — a hung get_fills() must not freeze the open-order side, and vice versa. Lifespan owns the task handle on `app.state.ib_activity_poller_task` and cancels + awaits it on shutdown.

  Two behaviors locked in by tests:
  - `register_from_snapshot` UPDATEs `snapshot-*` rows on price/qty drift and emits an `IB_MIRROR_UPDATE` order_event with before/after. Xenon-authored UUID rows are deliberately untouched (their `modify_sequence` invariant is the source of truth) — TWS-side modifies on those are a known gap.
  - `record_external_fills` resolves `(perm_id, scope)` → `submission_id` so blotter rows tie back to the originating order. Falls back to `legacy_id` grouping for true orphans only.

  Late-arriving commission/realizedPNL. IB delivers Execution then CommissionReport as separate messages — the report can lag by minutes for BAG. The poller inserts fills with commission=0 on the first tick, then applies `update_fill_commission` on subsequent ticks once `cr.execId == exec.execId`. Three tests lock this in: first-tick zero insert, second-tick non-zero update, idempotent zero-zero no-op.

  BAG execution rows. IB can report a combo as one BAG envelope plus separate option-leg executions for the same `permId`. The envelope carries combo net price/quantity and is not an economic leg. Rehydrate and aggregation must preserve `sec_type`, use the BAG envelope for order-level quantity/avg price, exclude it from trade leg/P&L math when option legs exist, and sum leg-level `realized_pnl` reports for closed combo history.

  **Known gap — TWS cancels not mirrored.** The current poller does not transition `WORKING` snapshot-* rows to `CANCELLED` when they disappear from `get_open_orders()`. Naïve disappearance-detection is unsafe: an order that *fills\* mid-tick also disappears, and we'd misclassify it as `CANCELLED`. The right fix combines the disappeared set with `xenon.order_fills` for the same `(perm_id, scope)` to disambiguate fill vs cancel, plus an idle-grace window. Tracked as follow-up.

## Read-Only Mode (`XENON_READ_ONLY=1`)

Set automatically by `scripts/infra/dev.sh live` so dev sessions can read live IB data without polluting `core_test` with live fills. Real live trading goes through the macmini Docker stack against `core_dev`. Full policy: root `CLAUDE.md` § Dev/Prod DB Split.

Helpers live in `xenon.api.guards`:

- `is_read_only()` — reads the env flag.
- `read_only_403()` — returns a `JSONResponse(status_code=403, content={"reason_code": "READ_ONLY_MODE", "detail": "..."})`. **Top-level `reason_code`** so the web toast helpers render it (toast reads `body.reason_code`, not `body.detail.reason_code` — see root memory `httpexception_dict_detail_breaks_toast`). Never wrap the same payload in `HTTPException(detail={...})`.

Surfaces that gate on the flag:

- **FastAPI write routes** — return 403 early: `POST /orders/place`, `POST /orders/cancel`, `POST /orders/modify` (`server.py`), `POST /journal` (`routes/journal.py`). `GET` siblings stay open.
- **Lifespan tasks** — `_run_rehydrate_on_boot`, `_run_fills_replay_on_boot`, and `_maybe_start_activity_poller` are skipped with a warning log when the flag is set. The poller task handle on `app.state.ib_activity_poller_task` is `None` in that case; shutdown logic already tolerates `None`.
- **Writer modules** — `xenon.execution.ib_sync._save_portfolio_to_postgres` and `_append_nav_snapshot` are no-ops. Any new persistence helper called from this package or from a subprocess CLI invoked by the lifespan must short-circuit on the flag, not silently write.

Tests: `src/xenon/api/tests/test_read_only_mode.py` (routes), `scripts/tests/test_ib_sync_read_only.py` (writer no-ops).

## Cancel / Modify Failure Propagation

1. **Cancel and modify MUST use subprocess with original clientId.**
   - IB scopes both `cancelOrder` and `placeOrder` (modify) by clientId.
   - Master client (clientId=0) can SEE all orders via `reqAllOpenOrders()` but CANNOT cancel/modify them (Error 10147 for cancel, Error 103 for modify).
   - The subprocess (`ib_order_manage.py`) detects the original clientId from `trade.order.clientId` and reconnects as that client before executing.
   - Pool-based cancel/modify does NOT work because orders are placed by subprocess clientIds (range 20-49), not pool clientIds (0-2).
2. **Clear VOL fields before modify.**
   - IB open-order snapshots may contain stale `volatility` and `volatilityType` values. Re-submitting on a non-VOL order causes Error 321.
   - Reset both to IB sentinel values (`1.7976931348623157e+308` and `2147483647`) before `placeOrder`.
3. **Do not trust the original IB `Trade` object as the only confirmation source.**
   - IB can confirm a cancel by removing the order from refreshed open orders without mutating the original `Trade` instance in place.
   - Cancel/modify flows must confirm against a refreshed open-order snapshot, not just the stale object reference.
4. **Treat disappearance after cancel as success.**
   - If the target order no longer appears in refreshed open orders after the cancel request, that is a valid IB acknowledgement.
5. **Preserve the real upstream error detail end to end.**
   - If a subprocess script exits non-zero with JSON on stdout, FastAPI must surface the human-readable `detail` / `message` / `error` field.
   - Next order routes must preserve upstream HTTP status/detail instead of collapsing provider failures to generic `500`s.
6. **Required regressions for cancel/modify bugs:**
   - Python/unit coverage for refreshed open-order confirmation semantics
   - route coverage for upstream status/detail propagation
   - browser coverage for the visible toast/error state
7. **Modify route advances `modify_sequence` BEFORE subprocess; on subprocess failure, the error response includes `applied_sequence` so the client counter can sync.**
   - Both the `result.ok=False` (503 `IB_CONNECTION`) and `status=="error"` (4xx/5xx by classification) branches echo `applied_sequence: <N>` in the HTTPException detail.
   - Without this, a client retry at the old N loses to the advanced DB counter → `MODIFY_STALE` loop.

## Client ID Allocation Rule

On-demand scripts MUST use `client_id="auto"` (range 20-49). Never hardcode — pool holds persistent connections on 0-9. Full range map in `docs/architecture/api-infrastructure.md`. Tests: `test_client_id_allocation.py` (17).

## Auth — Security-Relevant Behavior

**Auth-exempt paths:** `/health`, `/ws-ticket/validate`, `/docs`, `/openapi.json`.

**Localhost bypass:** Auth middleware and `verify_clerk_jwt` dependency skip validation for requests from `127.0.0.1`/`::1` (server-to-server). The WS relay also skips ticket validation for localhost connections. Enables local dev without Clerk sign-in.

**Graceful fallback:** When `CLERK_JWKS_URL` is not set, auth middleware passes all requests through.

Component map, files, ticket flow: `docs/architecture/api-infrastructure.md`.

## Dev probes (never enabled in production)

- `POST /dev/rehydrate/synthetic` — injects a synthetic PENDING row, runs rehydrate, returns event count. Gated on `XENON_API_TEST_MODE=1` OR `DEV_PROBES=1`. Used for observability readiness check before burn-in. Hidden from `/openapi.json` (`include_in_schema=False`); the gate is the real protection.

## DuckDB timestamp migration (one-time)

Prior to PR-C/D the `orders_store` writer did not pin the DuckDB session `TimeZone`. On non-UTC hosts, aware `datetime.now(timezone.utc)` values were converted to local wall-clock before being stripped of tzinfo — so pre-patch rows in `orders_submissions` are stored as _local_ wall-clock timestamps, while the post-patch `_submitted_at_epoch` reader treats every naive value as UTC.

Left unmigrated, the reader mis-ages those pre-patch PENDING rows by the host's UTC offset (e.g. +7h for America/Los_Angeles), which can cause fresh rows to be auto-`FAILED` with `PENDING_TIMEOUT`.

Run the one-time migration once per orders DB. Dry-run first:

```bash
uv run python scripts/migrations/2026_04_21_orders_submitted_at_to_utc.py \
    --from-tz America/Los_Angeles            # or whatever the host TZ was

# Review the preview, then:
cp data/orders.duckdb data/orders.duckdb.bak
uv run python scripts/migrations/2026_04_21_orders_submitted_at_to_utc.py \
    --from-tz America/Los_Angeles --apply
```

The script is **not idempotent** — it writes a sentinel `orders_events` row (`kind='MIGRATION_TZ_UTC_V1'`) on success and aborts on a second `--apply` run. Only servers deployed before the PR-C/D landing need this; new deploys start with the UTC-pinned writer.

## Health Check

```bash
curl http://localhost:8321/health
# Returns: ib_gateway, ib_pool (sync/orders/data), uw, futu, trading_mode,
# account, mode_verified, snapshotter, order_submissions, flex_divergence
```
