# src/xenon/api/ — CLAUDE.md

FastAPI bridge between Next.js and IB/UW/MenthorQ. Root `CLAUDE.md` is authoritative for policy. Infrastructure reference (files, ports, gateway modes, auth component map, deployment): `docs/architecture/api-infrastructure.md`.

**Read this before touching the order/cancel/modify/blotter/rehydrate path:** `docs/reference/order-path-incident-history.md` — chronological table of every non-trivial bug in this surface, with root cause, solution, and the regression test that protects against recurrence. Append a row when you ship a similar fix.

## Core rule

Next.js routes call FastAPI (`localhost:8321`) via `xenonFetch()` (`web/lib/xenonApi.ts`). **Never `spawn()`**. No spawn fallback — always try FastAPI first.

## Module Layout

- `server.py` — endpoint dispatch, IB pool lifecycle, background schedulers (CTA sync)
- `routes/` — per-topic FastAPI routers, included from `server.py`:
  - `uw_analyze.py` — `/uw-analyze/*` (portfolio bias, refresh, SSE streaming for progressive enrichment)
  - `uw_stats.py` — `/uw-stats`, `/uw-stats/reset`
  - `historical.py` — historical bars
- `services/` — business logic (stateful, testable without HTTP):
  - `uw_analyze_cache.py`, `uw_analyze_candidates.py`, `uw_analyze_daily_job.py`, `uw_analyze_diff.py`, `uw_analyze_flow_tracker.py`, `uw_analyze_oi_snapshots.py`, `uw_analyze_oi_tracker.py`, `uw_analyze_portfolio_bias.py`
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

**Public share routes** (no auth): `/api/regime/share`, `/api/vcg/share`, `/api/internals/share`, `/api/menthorq/cta/share`.

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
# Returns: ib_gateway, ib_pool (sync/orders/data), uw
```
