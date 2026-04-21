# src/xenon/api/ — CLAUDE.md

FastAPI bridge between Next.js and IB/UW/MenthorQ. Root `CLAUDE.md` is authoritative for policy. Infrastructure reference (files, ports, gateway modes, auth component map, deployment): `docs/architecture/api-infrastructure.md`.

## Core rule

Next.js routes call FastAPI (`localhost:8321`) via `xenonFetch()` (`web/lib/xenonApi.ts`). **Never `spawn()`**. No spawn fallback — always try FastAPI first.

## Module Layout

- `server.py` — endpoint dispatch, IB pool lifecycle, background schedulers (pre-market trend scan 8:30 AM ET, CTA sync)
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

- **Pre-market trend scan** — 8:30 AM ET weekdays, `xenon-trend-scan --top 25`, writes `data/trend_scan.json`. Defined as an asyncio loop started in the lifespan handler (`_trend_scan_premarket_loop`).
- **Futu singleton** — lazy-initialized on first `/futu/sync` call so the server boots even when OpenD is down. asyncio singleflight lock collapses concurrent fetches. 10s cooldown gate. **Uses a `None` sentinel (not `0.0`) for last-sync** — near process start `time.monotonic() - 0.0` would look recently-synced and serve stale cache.

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
python3.13 scripts/migrations/2026_04_21_orders_submitted_at_to_utc.py \
    --from-tz America/Los_Angeles            # or whatever the host TZ was

# Review the preview, then:
cp data/orders.duckdb data/orders.duckdb.bak
python3.13 scripts/migrations/2026_04_21_orders_submitted_at_to_utc.py \
    --from-tz America/Los_Angeles --apply
```

The script is **not idempotent** — it writes a sentinel `orders_events` row (`kind='MIGRATION_TZ_UTC_V1'`) on success and aborts on a second `--apply` run. Only servers deployed before the PR-C/D landing need this; new deploys start with the UTC-pinned writer.

## Health Check

```bash
curl http://localhost:8321/health
# Returns: ib_gateway, ib_pool (sync/orders/data), uw
```
