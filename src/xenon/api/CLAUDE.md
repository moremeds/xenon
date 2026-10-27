# src/xenon/api/ — CLAUDE.md

FastAPI bridge between Next.js and IB Gateway / Futu OpenD. Root `CLAUDE.md` is authoritative for policy. Infrastructure reference (files, ports, gateway modes, auth component map, deployment): `docs/architecture/api-infrastructure.md`.

**Read this before touching the order/cancel/modify/blotter/rehydrate path:** `docs/reference/order-path-incident-history.md` — chronological table of every non-trivial bug in this surface, with root cause, solution, and the regression test that protects against recurrence. Append a row when you ship a similar fix.

## Core rule

Next.js routes call FastAPI (`127.0.0.1:8321` in prod, `8421` under `dev.sh`) via `xenonFetch()` (`web/lib/xenonApi.ts`). **Never `spawn()`**. No spawn fallback — always try FastAPI first.

## Module Layout

- `server.py` — app assembly: lifespan (IB pool, order owner, boot reconcile, background tasks, shutdown budget), auth + read-only middleware, router includes, process-wide state (`app`, `ib_pool`, `order_owner`, `uw_available`, test mode) and the IB script runner (`_run_ib_script_with_recovery`). Route and service modules read this state as `server.<name>` through an in-function `from xenon.api import server` (never a top-level import: `server` imports every router). No re-exports of moved names — patch the module that defines the name.
- `routes/` — per-topic FastAPI routers, included from `server.py` (inclusion order = `app.routes` order):
  - `system.py` — `/health`, `/admin/operator`, `/dev/rehydrate/synthetic`, `/ws-ticket*`
  - `routes/portfolio.py` — `/attribution`, `/portfolio*`, `/orders/refresh`; owns the single-flight `_sync_task` and the PR-11 audit enforce hook
  - `routes/order_entry.py` — `/orders/{quote,place,cancel,replace,modify}` (thin handlers over `services/order_entry.py`)
  - `futu.py` — `/futu/sync`, `/futu/portfolio`; owns the Futu singleton
  - `routes/blotter.py` — `POST/GET /blotter`
  - `market.py` — `/options/{chain,expirations,greeks}`, `/market-depth`
  - `orders.py` — `GET /orders` (list) and `orders_payload_for_scope`
  - `historical.py` — historical bars / fills
  - `routes/journal.py` — trade journal entries
  - `trades.py` — closed trades
  - `wizard.py` — combo order wizard (plan/submit/reprice/abort/protect)
- `services/` — business logic (stateful, testable without HTTP):
  - `services/order_entry.py` — `place` / `cancel` / `modify` / `replace` cores (each starts with `ensure_order_allowed`), preflight, Gate 4, quote gate, owner-reply mapping (`order_error`). Callers: `routes/order_entry.py`, `combo_wizard/session.py` (submit/reprice/abort), `combo_wizard/protect.py` (the protect take-profit is a `place`: Gate 4 + reservation like any order), the audit hook (`_cancel_order`).
  - `advisory_lock.py` — Postgres advisory locks
  - `ib_activity_mirror.py` — IB→PG fills + open-orders mirror
  - `journal_auto_import.py` — PG-event-driven journal auto-import
- `ib_pool.py` — persistent IB connection pool: roles `sync` / `orders` / `data` = clientId base+3 / base+4 / base+5. Request-path quote / qualify work on `data` goes through `call_data_bounded` (see § IB quote deadline)
- `order_owner.py` — the single order owner: runs every place / cancel / modify / wizard protect on the `orders` connection (see § Order owner)
- `ws_ticket.py` — 30s single-use WebSocket auth tickets
- `ib_gateway.py` — docker/cloud/launchd gateway lifecycle
- `auth.py` — credential-only auth: internal token + path-scoped API keys (no localhost pass)

**New endpoint?** Add a router module under `routes/`, register it in `server.py`. Business logic goes in `services/`, not inline in the route.

## Background Tasks

- **Futu singleton** — lazy-initialized on first `/futu/sync` call so the server boots even when OpenD is down. asyncio singleflight lock collapses concurrent fetches. 10s cooldown gate. **Uses a `None` sentinel (not `0.0`) for last-sync** — near process start `time.monotonic() - 0.0` would look recently-synced and serve stale cache.
- **Single-leg rehydrate on boot (F7)** — `_run_rehydrate_on_boot()` runs synchronously inside the FastAPI `lifespan` before the server begins serving. Calls `xenon.execution.single_leg_rehydrate.rehydrate_on_boot()` (three-source reconcile: IB open orders, IB executions, positions-change flag; applied to the orders DB rows) with a 10s timeout; on timeout or failure we log a warning and continue. Skipped in test mode. Observability readiness check: `POST /dev/rehydrate/synthetic`.
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

## Futu scopes (SIMULATE / REAL switch, plan §11)

- Config: `FUTU_TRADE_ACC_ID_SIMULATE` (paper) and `FUTU_TRADE_ACC_ID_REAL` (live), each optional, exact nonzero digits (`execution/futu_scope.py`). The old `FUTU_TRADE_ACC_ID` + `FUTU_TRADE_ENV` configure nothing.
- Trading: `XENON_FUTU_TRADING` is a comma set over `simulate`, `real` (`execution/broker_capabilities.py`). Any other value, including the old `1`, is off for both, with one ERROR log. A FUTU place / cancel / modify needs its env in the set and configured.
- Request scope: the query parameter `account_env` = `paper` | `live` is the only wire name, GET and POST alike (`guards.futu_request_scope`). A read without it uses paper when SIMULATE is configured, else live; an invalid value is 400 `FUTU_SCOPE_INVALID`. A mutation without a valid one is 400 `FUTU_SCOPE_REQUIRED` (`ensure_order_allowed(..., account_env=)`). An env without an account reads empty lists (`scope_reason: "not_configured"`).
- Observation: `ObservationLoops` runs one `ObservationLoop` per exact scope (own thread, context, schedule, retirement): the configured scopes plus every stored FUTU scope with unresolved `order_submissions` rows, re-read each cycle, so a replaced or removed account keeps being reconciled read-only. `/health` futu lists them under `scopes`.
- Veto: while a stored scope of env E with unresolved rows is not the configured account of E, FUTU mutations for E get 403 `FUTU_SCOPE_CHANGE_PENDING` and one ERROR log. Toggling between the two configured scopes never triggers it.
- Orders: one Futu order worker (`api/futu_order_worker.py`) and one trade context serve both envs; each job carries its request scope, and the worker checks that scope with `get_acc_list` + `verify_live` before the core.
- Dispatch (`services/order_entry.py`, `?broker=FUTU&account_env=`): place (single stock / option; order types X5, contract `docs/architecture/futu-orders.md`), cancel and modify; replace / combo / wizard 403 `READ_ONLY_BROKER`; never an IB fallback.
  - Types and tuples: `futu_orders.allowed(env, sec, type, tif, session, op=)` = static Futu set ∩ operator allowlist (`XENON_FUTU_TUPLES_LIVE` / `_PAPER`, exact `SEC:TYPE:TIF:SESSION`; defaults = the v0.16 LMT surface, paper adds stock MKT) and, for modify, `XENON_FUTU_MODIFY_TYPES` (default `LMT`). Refused before any IO; cancel works for every type. Enabling a live tuple: runbook § 7 probe.
  - Place: guard → body fields of the type + precision (refused, never rounded) + `allowed()` → Gate 4 in the request scope (row carries `legs.futu_code`, the frozen fingerprint `legs.futu_intent` and current terms `legs.futu_terms`; `limit_price` NULL without a fixed limit) → IB quote gate per type (`quote_guard.check_terms`: LMT band; MKT regular session, spread ≤ 1 % of mid, `XENON_FUTU_MKT_NOTIONAL_CAP`; STP / MIT trigger side, no override; STP LMT / LIT trigger side + limit-vs-trigger band, overridable and audited in `PREFLIGHT_ACK_LIMIT` with the intent; trail bounds vs the IB mid; IB down → 503 `QUOTE_UNAVAILABLE`) → worker. A replay of one `clientAttemptId` is compared on the intent, never on the amended terms.
  - Cancel / modify name the order by `brokerOrderId` (string), looked up in the request scope only. Uncertain send = UNKNOWN, never re-sent; after an uncertain place only an exact-remark search whose order matches the intent may bind the row. Row changes after a send come only from fresh evidence through `orders_store.mirror_futu_order`; observation stores what Futu reports (type label, trigger, trail, session, raw status) in `legs.futu_order`, never in the intent or terms. WAITING_SUBMIT stays UNKNOWN (listed, demand kept).
  - Modify: body `newPrice` / `newQuantity` / `newAuxPrice` / `newTrailValue` / `newTrailSpread` (only fields of the type; type, trail type, session immutable). IB quote fetched first; on the worker: fresh read (an omitted field keeps Futu's current value; no read → nothing sent) → `allowed(op="modify")` → the place quote rules on the full outgoing terms (no override; a refusal sends nothing and opens no amend) → `gate4.begin_modify` reserves the full outgoing quantity under the scope lock → fence → send. It settles only when a fresh read shows every sent field on its grid (`futu_orders.amend_confirmed`; never a derived trailing price); `settle_modify` then updates quantity / limit / `futu_terms`, never `futu_intent`. Else the larger reservation stays and `MODIFY_PENDING` holds until an operator resolves it (runbook § 5; no automatic confirm or expiry — the SDK can dispatch after the send-by fence). An order made outside Xenon is managed from the fresh read; unreadable type or term → `FUTU_ORDER_NOT_MODIFIABLE`, cancel kept. Its session must be read: a reported session as is, N/A only with `fill_outside_rth` False = RTH, else refused. A trailing order whose read trail unit differs from (or lacks) the placed unit is refused the same way; confirmation also needs the same order type and trail unit. `legs.futu_order` keeps the unit and values of one broker read with that read's start (`observed_at`); an older read never replaces them, and `settle_modify` publishes the confirming read unless a newer one is stored (`orders_store.futu_read_is_newer`).
  - `/health` futu `trading: {paper, live}` = would a place pass the guard and reach a running worker (`order_entry.futu_trading_status`; live also needs a well-formed `FUTU_TRADE_PWD_MD5`); `capabilities[env] = {tuples, modify}` = what a client may offer (`order_entry.futu_capabilities`).

## Read-Only Mode (`XENON_READ_ONLY=1`)

Set automatically by `scripts/infra/dev.sh live` so dev sessions can read live IB data without polluting `core_test` with live fills. Real live trading goes through the macmini Docker stack against `core_dev`. Full policy: root `CLAUDE.md` § Dev/Prod DB Split.

Helpers live in `xenon.api.guards`:

- `is_read_only()` — reads the env flag.
- `read_only_403()` — returns a `JSONResponse(status_code=403, content={"reason_code": "READ_ONLY_MODE", "detail": "..."})`. **Top-level `reason_code`** so the web toast helpers render it (toast reads `body.reason_code`, not `body.detail.reason_code` — see root memory `httpexception_dict_detail_breaks_toast`). Never wrap the same payload in `HTTPException(detail={...})`.

Surfaces that gate on the flag:

- **FastAPI write routes** — return 403 early: `POST /orders/place`, `POST /orders/cancel`, `POST /orders/modify` (`routes/order_entry.py`), `POST /journal` (`routes/journal.py`). `GET` siblings stay open.
- **Lifespan tasks** — `_run_rehydrate_on_boot`, `_run_fills_replay_on_boot`, and `_maybe_start_activity_poller` are skipped with a warning log when the flag is set. The Wizard Risk Alert still evaluates but records no event. The poller task handle on `app.state.ib_activity_poller_task` is `None` in that case; shutdown logic already tolerates `None`.
- **Writer modules** — `xenon.execution.ib_sync._save_portfolio_to_postgres` and `_append_nav_snapshot` are no-ops. Any new persistence helper called from this package or from a subprocess CLI invoked by the lifespan must short-circuit on the flag, not silently write.
- **Writer CLIs** — `xenon-nav-flex-refresh`, `xenon-futu-statement-sync` and `flex_divergence_check --apply` exit 3 with a `READ_ONLY:` message before any DB or broker work.

Tests: `src/xenon/api/tests/test_read_only_mode.py` (routes), `scripts/tests/test_ib_sync_read_only.py` (writer no-ops), `scripts/tests/test_writer_entry_points_read_only.py` (writer CLIs).

## Runtime settings (`/admin/settings`)

Some env knobs have a DB override that applies without a restart. Registry, rules and cache: `src/xenon/runtime_settings.py` (one entry per key; the web renders from the API metadata, no TS copy). Tables: `xenon.runtime_settings` (one row per overridden key) and append-only `xenon.runtime_settings_audit` (migration `2026_10_27_runtime_settings`).

- **Effective value** = a valid DB row, else the env/default value read today. No row = today's behavior (`scripts/tests/test_runtime_settings.py::test_no_rows_consumers_equal_env_only`). An invalid or unknown row is ignored with an ERROR log.
- **Safety keys are narrow-only.** `XENON_FUTU_TRADING`, `XENON_FUTU_TUPLES_LIVE` / `_PAPER`, `XENON_FUTU_MODIFY_TYPES`: DB ⊆ env, effective = env ∩ DB. `XENON_FUTU_MKT_NOTIONAL_CAP`: DB ≤ env, effective = min (an invalid env cap still refuses every MKT). `XENON_GATE4_NETTING`: effective = env AND DB. A PUT that widens is 403 `SETTING_WIDENS_ENV`; the intersection still holds if the env is narrowed later. `XENON_AUDIT_CANCEL` may go on or off from the DB.
- **Typed confirm** (`confirm` = the key name, else 428 `SETTING_CONFIRM_REQUIRED`): turning `XENON_AUDIT_CANCEL` on, any change of the `real` token in `XENON_FUTU_TRADING`, any change of `XENON_FUTU_TUPLES_LIVE`. DELETE checks the same rule against the env value it reverts to.
- **`orders_paused`** (DB only, default false): `ensure_order_allowed` returns 403 top-level `reason_code: ORDERS_PAUSED` for the kinds in `guards._PAUSED_KINDS` (place, modify, replace, wizard submit / reprice / protect), right after the read-only check. IB and Futu, routes and in-process callers (wizard, protect) all pass through it. Cancel, wizard abort / plan / session create and the audit cancel are never paused.
- **Consumers** read the effective value at their existing call sites: `broker_capabilities.futu_trading_envs`, `futu_orders.allowed_tuples` / `modify_types`, `quote_guard.mkt_notional_cap`, `gate4.netting_enabled`, `audit_enforce.enabled`. The `env_*` siblings keep the pure env parse.
- **The api is the only DB reader (D5).** `xenon-ib-sync` calls `runtime_settings.env_only()` first: in that process every effective value is its env value, with no settings DB read. The api spawn (`routes/portfolio._run_sync_pass`) resolves `gate4.netting_enabled()` itself and passes it in the child env as `XENON_GATE4_NETTING=1|0`, so the child's naked-short audit uses the DB-narrowed value. A direct CLI run of `xenon-ib-sync` uses the shell env only (a DB override does not apply). Spawn helpers take `env=` (`run_entry_point`, `_run_ib_script_with_recovery`; extra variables on top of the api env).
- **Relay / futu-quotes keys** (`XENON_QUOTES_DEFAULT`, `XENON_FUTU_OPTIONS`, `IB_REALTIME_MAX_LINES`, `IB_REALTIME_STALE_TICK_MS`, `FUTU_QUOTE_QUOTA`, `FUTU_UNITS_PER_SYMBOL`, `FUTU_BOOK_STALE_S`, `FUTU_GREEK_FACTORS`): the api stores and serves only the DB value (`env_known: false`); the owning process combines it with its own env. The relay (`scripts/infra/ib_realtime/runtime_settings.js`) pulls `GET /admin/settings` every 10 s (`XENON_SETTINGS_POLL_MS`) from the api origin of `TICKET_VALIDATE_URL` with `X-Internal-Token`, keeps the last values when the api fails (start: env), applies its own keys live (quotes default and options re-route through `reconcileFutuRoutes`; a `MAX_LINES` shrink evicts view lines first then the idlest held, a grow fills free slots; `STALE_TICK_MS` on the next computation) and forwards the futu-quotes keys in a `config` frame (`docs/architecture/futu-quotes.md`). Relay `/status` reports `runtime_settings` (known, last_ok_at, last_error, overrides, effective). `last_error` and the pull-failure log carry only fixed codes (`no_internal_token`, `request_failed`, `http_<status>`, `bad_body`, `apply_failed`), never exception text: native fetch echoes an invalid header value, which can be the token.
- **Cache**: one SELECT per 5 s per process. A committed write of this process publishes its value into the cache at once (so a failing next read cannot fall back to the pre-write value) and a generation counter discards any load that started before it. A DB read error keeps the last known values (process start = env). Tests reset it per test (root `conftest.py`).
- **Writes** take `pg_advisory_xact_lock(0x52535431, hashtext(key))` before reading the old row, so concurrent first writes of one key serialize; the confirm rule and the audit `old_value` always see the committed predecessor. Numbers are validated without float conversion (a huge JSON int is 400 `SETTING_INVALID_VALUE`, and a stored one is ignored like any invalid row); greek factors are bounded to ±1e6.
- **Routes** (`routes/settings.py`): `GET /admin/settings`, `PUT /admin/settings/{key}` `{value, note?, confirm?}`, `DELETE /admin/settings/{key}?note=&confirm=`. `X-Internal-Token` only, enforced in `auth_middleware` before the generic 401 (`routes/settings.py::auth_refusal`): any `X-API-Key` header is 403 `API_KEY_FORBIDDEN` (valid key or not, internal token or not), dev-open, a wrong token or no credential is 401 `INTERNAL_TOKEN_REQUIRED`, both with top-level `reason_code`. PUT / DELETE are refused under `XENON_READ_ONLY=1` (403 `READ_ONLY_MODE`, before body parsing). Each write appends its audit row in the same transaction. Errors: 404 `SETTING_UNKNOWN`, 400 `SETTING_INVALID_VALUE`, 403 `SETTING_WIDENS_ENV`, 428 `SETTING_CONFIRM_REQUIRED`, all top-level.

## Order owner (PR-10)

Runbook: `docs/runbooks/pr10-order-owner-cutover.md`. Incident rows 37–40.

1. **One owner.** Place, cancel, modify and wizard protect run in-process on the pool `orders` connection (clientId `base + 4`) through `OrderOwner.run(core, timeout, settle)` on its pinned worker. No subprocess, no second IB connection, no reconnect as another clientId. The cores are `ib_place_order.place_order_on_client` and `ib_order_manage.cancel_order_on_client` / `modify_order_on_client`; only `services/order_entry.py` may import them (`scripts/checks/order_path_caller_allowlist.py`).
   - IB scopes `cancelOrder` and `placeOrder` (modify) by clientId: another client sees the order through `reqAllOpenOrders()` but cannot change it (Error 10147 cancel, Error 103 modify). So every Xenon order is placed by the owner, and `placing_client_id` stores the owner id.
   - **Positive routing:** an order whose recorded `placing_client_id` is not this environment's `orders` id (TWS / mobile / clientId 0, the old subprocess range, relay or pool ids, another environment) gets 409 `ORDER_NOT_OWNED` with no broker work. A row without a recorded id goes to the owner; its fresh scoped open-order snapshot refuses a foreign or ambiguous owner before any send.
2. **Invocation lifecycle:** queued → started → sent → done / abandoned. `settle` runs exactly once: on the worker for a started job (even after the request is gone), else on the request side.
   - Request deadline while queued → abandoned, zero sends (place: row FAILED, 503 `IB_CONNECTION`; modify: amend released).
   - Deadline after start → the request answers uncertain (place: 502 `PLACE_OUTCOME_UNKNOWN`, row UNKNOWN; cancel / modify: `amend_outcome: unconfirmed`). The worker still writes the final state. Worker final writes accept only PENDING / UNKNOWN (never a state the poller set from broker evidence); a late uncertain place only adds the ids it learned.
   - Any failure after the possible-send mark is unconfirmed, never not-sent. Only an explicit IB reject (`amend_outcome: "rejected"`) keeps a sent action terminal.
3. **Readiness before send.** The worker runs one fresh open-orders round trip before the core (READY only when that round trip completed with no readiness event in between). Right before every send a non-blocking check refuses on the halt latch or any readiness event since that round trip → 503 `IB_BROKER_DISCONNECTED` / `ORDERS_HALTED`, nothing sent. **Known limit:** the check only sees readiness events the orders worker has already processed; it is not an atomic network guarantee. A disconnect IB has not reported yet can still race a send, which is then reported as unconfirmed.
4. **Loss policy.** A supervisor owns the `orders` socket. A loss that is positively a gateway outage (1100/2110 before the loss, gateway port down, the whole pool down, or a gateway restart by the API) gets reconnects with backoff on the orders worker. Any other loss, or a clientId-in-use (326) at startup or during recovery, latches `ORDERS_HALTED`: zero reconnects, CRITICAL log, `/health` `status: orders_halted` (still HTTP 200), until the API restarts. Request paths never reconnect `orders`.
5. **Shutdown.** The lifespan's IB section runs first and cannot be skipped by later cleanup: owner stop (admission refused, queued jobs abandoned, supervisor cancelled), then `ib_pool.disconnect_all(timeout=35)`. The pool is marked closing first and a full close is terminal: a connect or reconnect that finishes later closes its own new socket instead of registering it, and a later `connect_all()` on the same pool connects nothing (one pool per api process; the gateway-restart path disconnects and reconnects role subsets and never closes the pool). Each role's disconnect is queued on its worker and is never cancelled: the 35 s bounds only the wait, and a disconnect behind a started order core runs when the core ends. Then the other cleanup steps (journal listener, poller, risk alert, Futu history loop, Futu observation loops, Futu order worker, Futu display clients, engine) each run with a small bound (sum 12 s). Budget: uvicorn drain 10 s + 35 s + 12 s = 57 s < 60 s compose `stop_grace_period` / `WATCHTOWER_TIMEOUT`. Limit: a core blocked on a broker call with no timeout of its own can outlive the budget; SIGKILL is then the backstop and its socket may stay open on the gateway (colima usernet, 2026-10-20; runbook stale-socket check).
6. **Reason codes** (top-level `reason_code`, mapped in `web/lib/orderReasonCodes.ts`): `ORDER_NOT_OWNED` 409, `IB_BROKER_DISCONNECTED` 503, `ORDERS_HALTED` 503, `IB_CONNECTION` 503 (place refused before send, row FAILED), `PLACE_OUTCOME_UNKNOWN` 502 (may be at IB; do not resubmit). The web treats a 5xx with a known code as terminal, except `PLACE_OUTCOME_UNKNOWN`.

## IB quote deadline and upstream state (incident 2026-10-26, row 43)

- **Deadline.** Every request-path quote / qualify call on the `data` role runs through `ib_pool.call_data_bounded` (callers: `services/order_entry.py` `_data_call` → `/orders/quote`, the place quote gate, Gate-4 leg meta; `routes/historical.py` `/contract/qualify`; the background wizard risk-alert combo snapshot with a 30 s budget). Historical bars / head-timestamp use `ib_pool.data_session`: lock wait + reconnect + qualify under the same deadline and fencing, then the download runs with the lock held and its own ib_async timeout. The Futu observation metadata resolver keeps its no-reconnect check but runs through `bounded_data_job` (15 s worker budget, below its 20 s caller timeout). One deadline, `XENON_IB_QUOTE_TIMEOUT_S` (default 10 s), covers the lock wait, a reconnect and the IB work. Past it: 503 with top-level `reason_code: QUOTE_UNAVAILABLE`. Place refuses in the quote gate, so nothing is sent.
- **Worker bound.** `bounded_data_job` runs on the role worker. It pumps the connection's events, refuses at once on readiness `lost` (503 `IB_BROKER_DISCONNECTED`), then sets `IB.RequestTimeout` to the time left for this job only and restores it after. ib_async then raises a timeout inside the worker, so the thread frees itself.
- **Wedged-worker recovery: fence, no reconnect.** A `data` job that is still running when its caller gives up fences the role (`IBPool.run_sync`). `acquire("data")` then fails fast (`IBRoleFenced`, 503 `QUOTE_UNAVAILABLE`) until the job returns. The fence is cleared by the job's done-callback. The reconnect inside `acquire` (`reconnect_role`: old-client disconnect, then `_connect_owned`) dispatches through the same fence, so an abandoned connect keeps the role fenced until it registers; a second caller never queues another connect on the same clientId. A queued job that never started is cancelled. The single worker means no two calls ever run on one client. The pool does not reconnect: during an upstream outage a new handshake times out too (observed 2026-10-26), and a reconnect resets readiness to `unknown`, which loses the `lost` signal. Limit: an IB call that ignores `RequestTimeout` keeps the role fenced until it returns (`/health` `ib_pool.data.fenced: true`). `_PoolContext` releases the role lock on any exit, CancelledError included.
- **Upstream state.** `/health` `ib_gateway.upstream` = `{state, since, last_code}` from `IBPool.upstream_status()`. The state is `lost` when any connected role last saw 1100 / 2110, `connected` when one is ready (round trip or 1102), else `unknown`. `since` = the earliest state entry in that group; `last_code` = the latest readiness event by event time (`IBClient.readiness_event_at`). A connection sees an event only when its loop runs, so the value can lag. While it is `lost`, top-level `status` is `degraded` (`orders_halted` still wins; HTTP 200). The web sidebar shows `NO IB SERVERS`.

## Naked-short audit enforce mode (PR-11)

`XENON_AUDIT_CANCEL=1` (default off; a rollout switch). `services/audit_enforce.py` runs after a successful `xenon-ib-sync --sync` inside `_sync_via_subprocess`, the single-flight sync task, so a trigger during enforcement joins it. Input: `ScriptResult.data["naked_short_audit"]` (the subprocess's last stdout line); unparseable or missing → no cancel.

- Never under `XENON_READ_ONLY` (and then no enforce health row is written), an owner that is missing or halted, a denied `ensure_order_allowed` (called explicitly: the in-process path skips route Depends), or an audit that is not `ok`/`violations` for the scope account.
- Cancels only orders this environment's owner placed (`client_id` = `pool_client_id("orders")`, positive `perm_id`) — exposure from TWS / mobile / other-client orders is reported, never cancelled. Identity (scope account, client_id, order_id, perm_id) goes to `_cancel_order` with the permId, so the core never falls back to orderId; a permId it cannot find (10147) or an order it reports `Order already Filled/Cancelled` is skipped (not an error) and ends the trigger.
- **Confirming pass before every cancel:** the sync re-runs once and the same identity must still be an eligible violation; else the target is dropped (`not_confirmed`, max 3 per trigger). One pass reads positions before open orders, so it can show a just-covered call as naked. Residual window: an IB position update that lags a fill by longer than the gap between the two passes.
- One cancel at a time, newest first; a confirmed cancel marks the row `CANCELLED` / `NAKED_SHORT_AUDIT` (`mark_terminal`, `expected_states=NON_TERMINAL_STATES`) and re-runs the sync before the next choice; uncertain / failed / a raised exception → stop, health `error`, nothing marked; max 5 cancels per trigger.
- **Duration:** enforcement holds the single-flight sync task. Worst case per trigger ≈ 5 × (30 s confirming pass + 15 s cancel + 30 s re-sync) + 3 × 30 s ≈ 8 min. `/portfolio/sync` callers (30 s web timeout) can time out meanwhile; the task keeps running (`asyncio.shield`), and background triggers answer `in_progress`.
- Health: `service_health("naked_short_audit")` with `mode: "enforce"`; keeps the latest audit's `skipped` / `audit_skipped` / `held_uncovered` / `held_short_stock`, and adds `cancelled`, `enforce_skipped`, `stopped`.

Tests: `scripts/tests/test_audit_enforce.py`.

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

## Health Check

```bash
curl http://localhost:8321/health
# Returns: status (ok | orders_halted), ib_gateway, ib_pool (sync/orders/data,
# each with connected + broker_readiness unknown|ready|lost), orders_owner
# (client_id, halted, halt_reason, recovering), uw, futu, trading_mode,
# account, mode_verified, snapshotter, order_submissions, flex_divergence,
# flex_token, realtime_subscribers
```
