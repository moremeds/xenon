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
- `ib_pool.py` — persistent IB connection pool: roles `sync` / `orders` / `data` = clientId base+3 / base+4 / base+5
- `order_owner.py` — the single order owner: runs every place / cancel / modify / wizard protect on the `orders` connection (see § Order owner)
- `ws_ticket.py` — 30s single-use WebSocket auth tickets
- `ib_gateway.py` — docker/cloud/launchd gateway lifecycle
- `auth.py` — credential-only auth: internal token + path-scoped API keys (no localhost pass)

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

  **TWS cancel mirroring (`sweep_disappeared_orders`).** Each poll tick, after the open-order sync and fills tick both succeed, the poller sweeps `WORKING`/`PARTIALLY_FILLED` rows that vanished from `get_open_orders()` (the fresh `reqAllOpenOrders()` response — never ib_async's `openTrades()` cache, which on the long-lived pool keeps other clients' cancelled orders as `Submitted` forever and made the mirror resurrect them; incident #35) and transitions them to `FILLED` (when `order_fills` for the same `(perm_id, scope)` cover the order quantity) or `CANCELLED` (`reason_code=TWS_CANCEL_MIRROR`, after the row is missing two consecutive sweeps — a one-tick grace, held in the module-level `_SWEEP_GRACE` set, so an order that fills mid-tick isn't misclassified). Safety guards: an **empty** snapshot while WORKING rows exist skips the whole sweep (a post-reconnect stale read must never mass-cancel); presence is matched on perm_id **OR** ib_order_id (the permId=0 race); a BAG with leg fills but no envelope row stays WORKING (ambiguous); a `fill_qty>0` guard stops a quantity-0 working row being marked FILLED on zero fills. The sweep runs only when both feeds succeeded that tick — a failed open-order fetch would otherwise mass-cancel. Implementation: `services/ib_activity_mirror.py::sweep_disappeared_orders`; tests: `scripts/tests/test_tws_cancel_sweep.py`.

- **Wizard Risk Alert** — `services/wizard_risk_alert.py`, started by `_maybe_start_wizard_risk_alert()` (replaces the deleted monitor daemon's `wizard_stop_monitor`). Every 30s (`XENON_WIZARD_RISK_ALERT_S`; `XENON_WIZARD_RISK_ALERT=0` disables) it lists PROTECTED sessions in the verified account scope whose protection has `alert_enabled`, quotes each combo through the ib_pool `data` role (`snapshot=True` per leg via `combo_quote_source`, run on the role thread with `pool.run_sync`), and applies the daemon rule `net_mid <= threshold`. A quote older than 2 min is `NO_QUOTE`, never a trigger. First crossing per lifecycle records `RISK_ALERT_CROSSED` in `xenon.wizard_events`. It **never places, cancels or modifies an order**. It runs under `XENON_READ_ONLY=1` but does not write. Skipped in test mode; `app.state.wizard_risk_alert_task` is cancelled + awaited on shutdown. Read via `GET /wizard/alerts` (live states + persisted events) → Next `/api/wizard/alerts` → `WizardRiskAlertBanner`.
- **Flex token expiry** — `services/flex_token.py`; `flex_token` field on `GET /health` and `/admin/operator` (Operator console tile). Same source and thresholds as the deleted daemon handler (`data/flex_token_config.json`, `reminder_days` 30/14/7/1); read-only.

## Read-Only Mode (`XENON_READ_ONLY=1`)

Set automatically by `scripts/infra/dev.sh live` so dev sessions can read live IB data without polluting `core_test` with live fills. Real live trading goes through the macmini Docker stack against `core_dev`. Full policy: root `CLAUDE.md` § Dev/Prod DB Split.

Helpers live in `xenon.api.guards`:

- `is_read_only()` — reads the env flag.
- `read_only_403()` — returns a `JSONResponse(status_code=403, content={"reason_code": "READ_ONLY_MODE", "detail": "..."})`. **Top-level `reason_code`** so the web toast helpers render it (toast reads `body.reason_code`, not `body.detail.reason_code` — see root memory `httpexception_dict_detail_breaks_toast`). Never wrap the same payload in `HTTPException(detail={...})`.

Surfaces that gate on the flag:

- **FastAPI write routes** — return 403 early: `POST /orders/place`, `POST /orders/cancel`, `POST /orders/modify` (`server.py`), `POST /journal` (`routes/journal.py`). `GET` siblings stay open.
- **Lifespan tasks** — `_run_rehydrate_on_boot`, `_run_fills_replay_on_boot`, and `_maybe_start_activity_poller` are skipped with a warning log when the flag is set. The Wizard Risk Alert still evaluates but records no event. The poller task handle on `app.state.ib_activity_poller_task` is `None` in that case; shutdown logic already tolerates `None`.
- **Writer modules** — `xenon.execution.ib_sync._save_portfolio_to_postgres` and `_append_nav_snapshot` are no-ops. Any new persistence helper called from this package or from a subprocess CLI invoked by the lifespan must short-circuit on the flag, not silently write.
- **Writer CLIs** — `xenon-nav-flex-refresh`, `xenon-futu-statement-sync` and `flex_divergence_check --apply` exit 3 with a `READ_ONLY:` message before any DB or broker work.

Tests: `src/xenon/api/tests/test_read_only_mode.py` (routes), `scripts/tests/test_ib_sync_read_only.py` (writer no-ops), `scripts/tests/test_writer_entry_points_read_only.py` (writer CLIs).

## Order owner (PR-10)

Runbook: `docs/runbooks/pr10-order-owner-cutover.md`. Incident rows 37–40.

1. **One owner.** Place, cancel, modify and wizard protect run in-process on the pool `orders` connection (clientId `base + 4`) through `OrderOwner.run(core, timeout, settle)` on its pinned worker. No subprocess, no second IB connection, no reconnect as another clientId. The cores are `ib_place_order.place_order_on_client` and `ib_order_manage.cancel_order_on_client` / `modify_order_on_client`; only `server.py` may import them (`scripts/checks/order_path_caller_allowlist.py`).
   - IB scopes `cancelOrder` and `placeOrder` (modify) by clientId: another client sees the order through `reqAllOpenOrders()` but cannot change it (Error 10147 cancel, Error 103 modify). So every Xenon order is placed by the owner, and `placing_client_id` stores the owner id.
   - **Positive routing:** an order whose recorded `placing_client_id` is not this environment's `orders` id (TWS / mobile / clientId 0, the old subprocess range, relay or pool ids, another environment) gets 409 `ORDER_NOT_OWNED` with no broker work. A row without a recorded id goes to the owner; its fresh scoped open-order snapshot refuses a foreign or ambiguous owner before any send.
2. **Invocation lifecycle:** queued → started → sent → done / abandoned. `settle` runs exactly once: on the worker for a started job (even after the request is gone), else on the request side.
   - Request deadline while queued → abandoned, zero sends (place: row FAILED, 503 `IB_CONNECTION`; modify: amend released).
   - Deadline after start → the request answers uncertain (place: 502 `PLACE_OUTCOME_UNKNOWN`, row UNKNOWN; cancel / modify: `amend_outcome: unconfirmed`). The worker still writes the final state. Worker final writes accept only PENDING / UNKNOWN (never a state the poller set from broker evidence); a late uncertain place only adds the ids it learned.
   - Any failure after the possible-send mark is unconfirmed, never not-sent. Only an explicit IB reject (`amend_outcome: "rejected"`) keeps a sent action terminal.
3. **Readiness before send.** The worker runs one fresh open-orders round trip before the core (READY only when that round trip completed with no readiness event in between). Right before every send a non-blocking check refuses on the halt latch or any readiness event since that round trip → 503 `IB_BROKER_DISCONNECTED` / `ORDERS_HALTED`, nothing sent. **Known limit:** the check only sees readiness events the orders worker has already processed; it is not an atomic network guarantee. A disconnect IB has not reported yet can still race a send, which is then reported as unconfirmed.
4. **Loss policy.** A supervisor owns the `orders` socket. A loss that is positively a gateway outage (1100/2110 before the loss, gateway port down, the whole pool down, or a gateway restart by the API) gets reconnects with backoff on the orders worker. Any other loss, or a clientId-in-use (326) at startup or during recovery, latches `ORDERS_HALTED`: zero reconnects, CRITICAL log, `/health` `status: orders_halted` (still HTTP 200), until the API restarts. Request paths never reconnect `orders`.
5. **Shutdown.** The lifespan's IB section runs first and cannot be skipped by later cleanup: owner stop (admission refused, queued jobs abandoned, supervisor cancelled), then `ib_pool.disconnect_all(timeout=35)`. The pool is marked closing first, so a reconnect that finishes later closes its own new socket instead of registering it. Each role's disconnect is queued on its worker and is never cancelled: the 35 s bounds only the wait, and a disconnect behind a started order core runs when the core ends. Then the other cleanup steps (journal listener, poller, risk alert, Futu, engine) each run with a small bound (sum 10 s). Budget: uvicorn drain 10 s + 35 s + 10 s < 60 s compose `stop_grace_period` / `WATCHTOWER_TIMEOUT`. Limit: a core blocked on a broker call with no timeout of its own can outlive the budget; SIGKILL is then the backstop and its socket may stay open on the gateway (colima usernet, 2026-10-20; runbook stale-socket check).
6. **Reason codes** (top-level `reason_code`, mapped in `web/lib/orderReasonCodes.ts`): `ORDER_NOT_OWNED` 409, `IB_BROKER_DISCONNECTED` 503, `ORDERS_HALTED` 503, `IB_CONNECTION` 503 (place refused before send, row FAILED), `PLACE_OUTCOME_UNKNOWN` 502 (may be at IB; do not resubmit). The web treats a 5xx with a known code as terminal, except `PLACE_OUTCOME_UNKNOWN`.

## Cancel / Modify Semantics

1. **Clear VOL fields before modify.**
   - IB open-order snapshots may contain stale `volatility` and `volatilityType` values. Re-submitting on a non-VOL order causes Error 321.
   - Reset both to IB sentinel values (`1.7976931348623157e+308` and `2147483647`) before `placeOrder`.
2. **Do not trust the original IB `Trade` object as the only confirmation source.**
   - IB can confirm a cancel by removing the order from refreshed open orders without mutating the original `Trade` instance in place.
   - Cancel/modify flows must confirm against a refreshed open-order snapshot, not just the stale object reference.
3. **Treat disappearance after cancel as success.** A sent cancel with no acknowledgement and no disappearance is unconfirmed (503 `IB_CONNECTION`, uncertain), never an IB reject.
4. **Preserve the real upstream error detail end to end.**
   - FastAPI surfaces the core's human-readable `message` as `detail` with a top-level `reason_code`.
   - Next order routes must preserve upstream HTTP status/detail instead of collapsing provider failures to generic `500`s.
5. **Required regressions for cancel/modify bugs:**
   - Python/unit coverage for refreshed open-order confirmation semantics
   - route coverage for upstream status/detail propagation
   - browser coverage for the visible toast/error state
6. **Modify route advances `modify_sequence` BEFORE the owner runs; every failure response includes `applied_sequence` so the client counter can sync.**
   - Without this, a client retry at the old N loses to the advanced DB counter → `MODIFY_STALE` loop.

## Client ID Allocation Rule

Each environment owns a block `XENON_IB_CLIENT_ID_BASE` (0-800, step 100; prod 0, `dev.sh` 100). Inside the block: pool base+3/4/5, relay base+10..19, `client_id="auto"` base+20..49 (read/data scripts only — no order writes). `IBClient.connect` refuses an id outside the caller's zone; the api refuses a base that does not match its database. Never hardcode an id. Full map: `docs/architecture/api-infrastructure.md`; deployment contract: `docs/runbooks/pr10-order-owner-cutover.md`. Tests: `scripts/tests/test_client_id_allocation.py`.

## Auth — Security-Relevant Behavior

**Auth-exempt paths:** `/health` only. (`/docs`, `/openapi.json`, and `/ws-ticket/validate` are NOT exempt — `/docs`/`/openapi.json` expose the full trading API schema, and the Node relay authenticates `/ws-ticket/validate` with `X-Internal-Token`.)

**Fail-closed, credential-only.** `auth.classify_auth(request)` (sync) is called by `server.py::auth_middleware`. A non-exempt request is DENIED (401) unless it proves identity, in order:

1. Valid `X-Internal-Token` == `XENON_INTERNAL_API_TOKEN` → pass, full access. `xenonFetch` AND the direct web→api callers attach it via `internalApiHeaders()`. Checked before API keys.
2. Valid `X-API-Key`: `XENON_QUERY_API_KEY` → read-only data + market-data paths (`QUERY_API_KEY_PATHS`): GET reads (`/portfolio`, `/orders`, `/orders/quote`, `/options/chain`, `/options/expirations`, `/options/greeks`, `/market-depth`, `/performance`, `/watchlist`, …) plus body-carrying **read-only** POSTs (`/historical/bars`, `/historical/head-timestamp`, `/contract/qualify`, `/ws-ticket`); `MDW_API_KEY` → `/historical/*` + `/contract/qualify` (`API_KEY_ALLOWED_PATHS`). A matched key grants-or-denies definitively; neither grants write/sync paths. Consumer reference: `docs/reference/readonly-query-api.md`.
3. `XENON_AUTH_ALLOW_DEV_OPEN=1` (strict truthy) → pass. **Dev/test only** — set it yourself for debugging; `dev.sh` does not export it. Production never sets it, so a prod env that fails to load denies rather than exposes the API.
4. Else → 401.

There is **no peer-address authentication**: requests from `127.0.0.1`/`::1` get no pass — Tailnet peers, Docker bridges, and proxies can all reach loopback. `/health` is exempt; `/ws-ticket` requires a credential (internal token or scoped key) and mints 30s single-use tickets; `/ws-ticket/validate` requires the internal token (the Node relay sends it).

`validate_auth_config()` runs at lifespan startup: refuses to boot if `XENON_INTERNAL_API_TOKEN == XENON_QUERY_API_KEY` (a leaked read-only key could otherwise be replayed for write access) and logs the posture (ENFORCED / DEV-OPEN / FAIL-CLOSED).

Order-write endpoints (`/orders/place|cancel|modify`, `/portfolio/sync`, `POST /blotter`, `POST /futu/sync`) are in no API-key allowlist, so an external key can never reach them — only the internal token.

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
# Returns: status (ok | orders_halted), ib_gateway, ib_pool (sync/orders/data,
# each with connected + broker_readiness unknown|ready|lost), orders_owner
# (client_id, halted, halt_reason, recovering), uw, futu, trading_mode,
# account, mode_verified, snapshotter, order_submissions, flex_divergence,
# flex_token, realtime_subscribers
```
