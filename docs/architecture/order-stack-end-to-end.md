# Order Stack — End-to-End Architecture

**Audience:** A senior engineer joining the project tomorrow who needs to understand every layer the order stack passes through, from the React click to the IB acknowledgement to the Postgres row, including the failure modes and the durable guards that exist because of past incidents.

**Companion file:** `order-stack-end-to-end.html` renders the same content with Mermaid sequence/state diagrams. Open via `open docs/architecture/order-stack-end-to-end.html`.

**Foundational reads (load these into context first):**

- `CLAUDE.md` (project root) — Mandatory rules, four gates, broker-account scope policy.
- `web/CLAUDE.md` — pricing/P&L correctness rules, BAG leg convention, naked-short layers.
- `src/xenon/api/CLAUDE.md` — FastAPI surface, IB activity mirror, cancel/modify clientId semantics.
- `src/xenon/CLAUDE.md` — naked-short table, combo guardrails, broker scope.
- `docs/reference/order-path-incident-history.md` — chronological log of every non-trivial bug. Read before touching this surface.

> **TL;DR** — The path is `OrderTab → /api/orders/place (Next.js) → POST /orders/place (FastAPI) → 8-stage validation pipeline → subprocess (xenon-ib-place-order) → IB Gateway → ack persisted via orders_store.mark_submitted → activity poller mirrors fills/external-modify back to Postgres → UI re-renders from the next /orders GET (no live WS push for order events; WS is price-only)`. The most fragile parts are: (1) **no continuous PENDING reaper** — hangs leave phantoms until restart (§13.11), (2) **subprocess concurrency unbounded** — clientId range 20–49 exhaustible under load (§13.12), (3) the in-process bypass class — `_orders_*_from_body` and the `submit_combo` parallel route both skip route-level Depends or reproduce gates inline (§13.13), (4) the cancel/modify clientId scoping — pool can see but not cancel, (5) **fill-to-toast latency up to ~120 s** from double-polling — order-event WS push would compress this to sub-second (§13.15), (6) the late-arriving CommissionReport coupling, and (7) the TWS-cancel-not-mirrored gap in the activity poller.

---

## 1. Layer Map

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 1 — React UI                                                      │
│   web/components/ticker-detail/OrderTab.tsx (NewOrderForm,              │
│   ComboOrderForm, ExistingOrderRow), ModifyOrderModal, RegimeBlockModal │
│   web/lib/OrderActionsContext.tsx (cancel/modify imperative API)        │
│   web/lib/order/regimeGate.ts (parseRegimeGateResponse + helpers)       │
│   web/lib/nakedShortGuard.ts (Gate 4 client-side mirror)                │
│   web/lib/orderReasonCodes.ts (reason_code → toast copy)                │
└─────────────────────────────────────────────────────────────────────────┘
                                  │ fetch("/api/orders/{place,modify,cancel}")
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 2a — Next.js App Router (proxy only, no business logic)           │
│   web/app/api/orders/place/route.ts                                     │
│   web/app/api/orders/modify/route.ts                                    │
│   web/app/api/orders/cancel/route.ts                                    │
│   web/app/api/orders/quote/route.ts                                     │
│   web/lib/xenonApi.ts (xenonFetch — preserves upstream JSON detail)     │
└─────────────────────────────────────────────────────────────────────────┘
                                  │ xenonFetch → http://localhost:8321
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 2b — FastAPI (src/xenon/api/server.py)                            │
│   @app.post("/orders/place") → orders_place → _orders_place_from_body   │
│   @app.post("/orders/modify") → orders_modify → _orders_modify_from_body│
│   @app.post("/orders/cancel") → orders_cancel → _orders_cancel_from_body│
│   @app.get ("/orders/quote") → orders_quote                             │
│   Validation pipeline (8 stages — §4)                                   │
│   Subprocess dispatch via _run_ib_script_with_recovery                  │
└─────────────────────────────────────────────────────────────────────────┘
                                  │ asyncio.subprocess "xenon-ib-place-order"
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 3 — Execution scripts (subprocess, fresh ib_async connection)    │
│   src/xenon/execution/ib_place_order.py (place — clientId range 20–49)  │
│   src/xenon/execution/ib_order_manage.py (cancel/modify — original cid) │
│   src/xenon/execution/regime_gate.py (lives under api/services, see §6) │
│   src/xenon/execution/preflight.py (Gate 4 server-side)                 │
│   src/xenon/execution/orders_store.py (Postgres facade)                 │
│   src/xenon/execution/quote_guard.py, naked_short_audit.py              │
│   src/xenon/execution/account_scope.py (env→AccountScope resolver)      │
│   src/xenon/clients/ib_client.py (IBClient — connect, qualify, place)   │
└─────────────────────────────────────────────────────────────────────────┘
                                  │ ib_async (TCP 127.0.0.1:4002)
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 4 — IB Gateway / TWS                                              │
│   docker container OR Tailscale-bridged VPS gateway                     │
│   placeOrder → openOrder → orderStatus(Submitted) → orderStatus(Filled) │
│   execDetails + commissionReport (separate messages, lag possible)      │
│   Error event channel — codes 110 (off-tick), 201 (BAG reversal),       │
│   10147/10148 (cancelled-not-found), 321 (VOL fields)                   │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼ ib_activity_poller (60 s)
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 5 — Postgres (`xenon` schema)                                     │
│   order_submissions  — primary order ledger (PK submission_id)          │
│   order_events       — append-only audit (kind, detail JSONB)           │
│   order_fills        — execution-grain fills (PK exec_id)               │
│   regime_overrides   — composite scope FK to order_submissions          │
│   account_snapshots  — net_liquidation source for bankroll              │
│   trades             — aggregated trade rollup                          │
│   nav_history        — composite PK (broker, env, account, date)        │
│   wizard_combo_attempts — combo-wizard structure attempts               │
│   events.outbox      — fill.recorded / fill.commission_updated channel  │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼ next /orders GET → /api/orders → UI
                              (UI feedback — §11)
```

The flow is **strictly one-way for order events.** There is no live push from FastAPI to the browser for order acknowledgements; the UI re-fetches `/orders` after every place/modify/cancel call, plus periodically via `useIbOrders`-style polling. The only push channel is the price WebSocket (`web/lib/usePrices.ts:354`, `web/lib/IBStatusContext.tsx:120`) — distinct from the order surface.

---

## 2. Source Files By Layer (Cite-able Manifest)

### Layer 1 — Frontend (web/)

| File                                                   | Purpose                                                                                                       | Key symbols                                                                                                                                   |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `web/components/ticker-detail/OrderTab.tsx` (1712 LOC) | Single-file home of `NewOrderForm`, `ComboOrderForm`, `ExistingOrderRow`, retry-with-override flows           | `handlePlace`, `retryRegimeOrder`, `handleRegimeOverride`, `handleRegimeResize`, `buildSingleLegOrderPayload`, `legsWithActions`, `netPrices` |
| `web/components/ModifyOrderModal.tsx`                  | Modify dialog (price/qty, BAG vs single)                                                                      | `resolveOrderPriceData`, `useModifyForm`                                                                                                      |
| `web/components/RegimeBlockModal.tsx` (231 LOC)        | Override/resize prompt for 409/422 regime responses                                                           | renders block reason, captures `override_reason ≥ 10 chars`                                                                                   |
| `web/lib/order/regimeGate.ts`                          | Response parser for 409 REGIME_BLOCK / 422 REGIME_RESIZE_REQUIRED                                             | `parseRegimeGateResponse`, `buildRegimeOverrideFields`, `suggestResizeQuantity`, `isRegimeOverrideReasonValid`                                |
| `web/lib/order/placeOrderContract.ts`                  | Body builder used by Next.js route                                                                            | `buildFastApiPlaceOrderPayload`                                                                                                               |
| `web/lib/order/types.ts`                               | UI-side order types                                                                                           | `OrderAction`, `OrderTif`, `OrderPrices`, `OrderLeg`, `OrderSummary`                                                                          |
| `web/lib/OrderActionsContext.tsx`                      | Imperative cancel/modify API for non-form callers (e.g. ExistingOrderRow)                                     | `requestCancel`, `requestModify`, `pendingCancels`, `pendingModifies`                                                                         |
| `web/lib/nakedShortGuard.ts`                           | UI-side mirror of Python `preflight.evaluate_combo` Gate 4                                                    | `checkNakedShortRisk`                                                                                                                         |
| `web/lib/orderReasonCodes.ts` (114 LOC)                | Single-source map `reason_code → toast copy` (must stay in parity with `ReasonCode` enum, see line 1 comment) | `ORDER_REASON_CODES`, `getReasonToast`                                                                                                        |
| `web/lib/xenonApi.ts` (49 LOC)                         | Minimal fetch helper; preserves upstream JSON `detail` and parsed body on `XenonApiError`                     | `xenonFetch`, `XenonApiError`                                                                                                                 |
| `web/lib/usePrices.ts:354,515`                         | Price WebSocket — feeds bid/ask used in form `netPrices` calc                                                 | `WebSocket(url)`                                                                                                                              |

### Layer 2a — Next.js routes

| File                                           | Purpose                                                                                | Notes                                                         |
| ---------------------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `web/app/api/orders/place/route.ts` (252 LOC)  | Validates body, calls FastAPI, maps silent IB cancel/inactive → 502 UPSTREAM_ERROR     | `runtime = "nodejs"`, `ORDER_PLACE_TIMEOUT_MS = 60_000`       |
| `web/app/api/orders/modify/route.ts` (286 LOC) | Single-leg modify or **cancel-then-place** for combo restructure (see §10 + risk note) | `replaceOrder` branch, `isModifyConfirmed`                    |
| `web/app/api/orders/cancel/route.ts` (56 LOC)  | Pure proxy to FastAPI + refresh + GET /orders                                          | Best-effort refresh — non-fatal on failure                    |
| `web/app/api/orders/quote/route.ts` (21 LOC)   | Pure proxy to `/orders/quote`                                                          | Mints signed `quote_token` (currently unused by UI — see §13) |

### Layer 2b — FastAPI

| File                                              | Purpose                                                                  |
| ------------------------------------------------- | ------------------------------------------------------------------------ |
| `src/xenon/api/server.py` (4162 LOC)              | All order/cancel/modify routes; validation pipeline; subprocess dispatch |
| `src/xenon/api/services/regime_gate.py` (359 LOC) | RegimeGate logic + `evaluate_order_gate` helper used by routes           |
| `src/xenon/api/services/regime_state.py`          | Resolves the per-scope `RegimeState` (binding tier from VCG + CRI)       |
| `src/xenon/api/services/ib_activity_mirror.py`    | Background fills/open-order poller (60 s default cadence)                |
| `src/xenon/api/auth.py`                           | Clerk JWT + API key dependency; localhost bypass                         |
| `src/xenon/api/ib_pool.py`                        | Persistent IB connection pool (clientId 0–9)                             |

### Layer 3 — Execution

| File                                              | Purpose                                                                                                | Notes                                                                                                                                                                                                                       |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/xenon/execution/ib_place_order.py` (241 LOC) | The ONLY caller-allowlisted place subprocess (`scripts/checks/order_path_caller_allowlist.py`)         | clientId range 20–49 via `client_id="auto"`                                                                                                                                                                                 |
| `src/xenon/execution/ib_order_manage.py`          | Cancel + modify subprocess; reconnects as the **original** clientId (see Layer 4 note)                 |                                                                                                                                                                                                                             |
| `src/xenon/execution/preflight.py` (456 LOC)      | Server-side Gate 4 — pure function over `PortfolioView`                                                | `evaluate`, `evaluate_combo`, `combo_close_covered_by_portfolio`, `combo_uncovered_short_call_ratio`                                                                                                                        |
| `src/xenon/execution/orders_store.py` (890 LOC)   | Postgres facade. PR #75 stripped `db_path=` arg; signature is positional + scope kwargs                | `reserve_attempt`, `mark_submitted`, `mark_terminal`, `apply_modify`, `apply_modify_by_perm_id`, `record_fill`, `update_fill_commission`, `register_from_snapshot`, `lookup_submission_id_by_*`, `working_reservations_for` |
| `src/xenon/execution/quote_guard.py` (157 LOC)    | Quote-token verify + tick-grid + limit-band + market-hours; `check_payload` has **no freshness check** | `check`, `check_payload`, `TickRuleCache`                                                                                                                                                                                   |
| `src/xenon/execution/account_scope.py` (78 LOC)   | Frozen `AccountScope(broker, account_env, broker_account)` from app.state or env                       | `resolve_from_app_state`, `resolve_from_env`                                                                                                                                                                                |
| `src/xenon/execution/naked_short_audit.py`        | Post-sync cancel of any open order that violates Gate 4                                                | Runs after every `ib_sync` (see §13.4)                                                                                                                                                                                      |
| `src/xenon/execution/single_leg_rehydrate.py`     | Boot reconcile (orders DB + IB open orders + CRI monitor)                                              | `rehydrate_on_boot` (10 s timeout in lifespan)                                                                                                                                                                              |
| `src/xenon/execution/quote_tokens.py`             | HMAC-signed quote token mint/verify                                                                    | UI no longer attaches tokens (see §13.3)                                                                                                                                                                                    |
| `src/xenon/clients/ib_client.py` (~830 LOC)       | `IBClient` wrapper: connect, `qualify_contracts`, `place_order`, `cancel_order`                        | clientId range definitions                                                                                                                                                                                                  |

### Layer 5 — Postgres schema

| Table                         | PK / Key                                             | Scope columns                                    | FK                                                                                                                                                                                                                                                                                                                                                               |
| ----------------------------- | ---------------------------------------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `xenon.order_submissions`     | `submission_id` (uuid text)                          | `(broker, account_env, broker_account)` not null | —                                                                                                                                                                                                                                                                                                                                                                |
| `xenon.order_events`          | `event_id` BIGSERIAL                                 | none — joins via FK                              | `submission_id → order_submissions.submission_id`                                                                                                                                                                                                                                                                                                                |
| `xenon.order_fills`           | `exec_id` (IB execId)                                | `(broker, account_env, broker_account)` not null | `submission_id → order_submissions.submission_id` (nullable), `combo_attempt_id → wizard_combo_attempts` (nullable). CHECK ensures one-of-three is present (`ck_fills_source_present`).                                                                                                                                                                          |
| `xenon.regime_overrides`      | `id` BIGSERIAL                                       | full 4-tuple                                     | **Composite FK** `(submission_id, broker, account_env, broker_account) → order_submissions.{same}` `DEFERRABLE INITIALLY DEFERRED` (`fk_regime_overrides_submission_scope`, `c4d5e6f70123_add_regime_overrides_table.py:62`). The matching UNIQUE on `order_submissions(submission_id, broker, account_env, broker_account)` is `uq_order_sub_submission_scope`. |
| `xenon.account_snapshots`     | `(broker, account_env, broker_account, snapshot_at)` | full 4-tuple                                     | —                                                                                                                                                                                                                                                                                                                                                                |
| `xenon.nav_history`           | `(broker, account_env, broker_account, date)`        | full 4-tuple                                     | —                                                                                                                                                                                                                                                                                                                                                                |
| `xenon.trades`                | `id`                                                 | full 4-tuple                                     | `submission_id` (nullable)                                                                                                                                                                                                                                                                                                                                       |
| `xenon.wizard_combo_attempts` | `attempt_id`                                         | full 4-tuple                                     | `session_id`                                                                                                                                                                                                                                                                                                                                                     |
| `events.outbox`               | `id`                                                 | —                                                | —                                                                                                                                                                                                                                                                                                                                                                |

Important invariants (see `src/xenon/db/schema.py:222–248`):

- `CHECK ck_order_sub_broker_ib_only` — only IB orders. Futu is read-only.
- `CHECK ck_order_sub_account_env IN ('paper','live','sim','legacy_unknown')`.
- `UNIQUE uq_order_sub_user_attempt(broker, account_env, broker_account, user_id, client_attempt_id)` — the idempotency key. `reserve_attempt` uses ON CONFLICT DO NOTHING against this.
- `UNIQUE uq_order_sub_submission_scope(submission_id, broker, account_env, broker_account)` — the FK target for `regime_overrides`. Logically redundant under the PK, but Postgres requires an explicit UNIQUE for composite FKs (`schema.py:235–244`).

---

## 3. Sequence — Single-leg LIMIT BUY (Happy Path)

User clicks **BUY 1 AAPL Call** in the order form. The trace:

### 3.1 React state → POST body

1. Form fields are local React state in `NewOrderForm` (`OrderTab.tsx:347`):
   - `action` (default: `BUY` if no position, else `SELL`) — `OrderTab.tsx:364–365`
   - `quantity`, `limitPrice`, `tif` (`DAY`/`GTC`)
   - `attemptId` from `useClientAttemptId({ ticker })` — generates a stable UUIDv4 per ticket; resets to a new UUID after `markTerminal()` (`useClientAttemptId.ts`).
2. Bid/ask drives the price strip: `tickerPriceData?.bid/ask`, with `mid = (bid + ask) / 2` (`OrderTab.tsx:360–362`).
3. `nakedShortWarning` is recomputed reactively on action change (`OrderTab.tsx:420`). When `action === "SELL"`, `checkNakedShortRisk` (UI mirror of `preflight.evaluate_combo`) runs.
4. `handlePlace` (`OrderTab.tsx:436`):
   - First click: `setConfirmStep(true)` and returns — the UI shows the confirm summary.
   - Second click: builds payload via `buildSingleLegOrderPayload` (`OrderTab.tsx:286`):
     - `type: "option"`, `expiry` (YYYYMMDD; dashes stripped at `:307`), `strike`, `right` (`C`/`P`), `con_id` if known.
   - Re-runs `checkNakedShortRisk` defensively (`:458`). On block, sets local error and returns.
   - `attemptId.markSubmitted()` flips local state so the same UUID is re-sent on regime retries.
   - `fetch("/api/orders/place", { method: "POST", body: JSON.stringify({ ...payload, client_attempt_id: attemptId.id }) })` (`:473–477`).

### 3.2 Next.js route — `web/app/api/orders/place/route.ts`

1. Schema-validate via `firstPlaceOrderSchemaErrorMessage(parsed)` (`:78`).
2. Normalize CALL/PUT → C/P (`:95–103`) — chain UI sends long form; IB and naked-short guard expect single letter.
3. Re-validate quantity > 0, limitPrice > 0 (or non-zero for combo, `:137–157`). For combo, signed prices are valid (credit/debit preserved per `web/CLAUDE.md` Credit/Debit Sign Convention).
4. `buildFastApiPlaceOrderPayload(body)` (`placeOrderContract.ts:6`) — copies fields, normalizes right, attaches `client_attempt_id`, `quote_token` (UI doesn't set this in production — §13.3), `con_id`, `acknowledge_limit_override`.
5. `xenonFetch("/orders/place", { method: "POST", body, timeout: 60_000 })` (`:188`). `xenonFetch` reads `XENON_API_URL` (default `http://localhost:8321`), attaches Bearer token if present, sets `cache: "no-store"`.
6. On non-2xx, `xenonFetch` parses upstream JSON and constructs `XenonApiError(status, detail, body)` where `detail` reads `body.detail` first then `body.error` then stringifies (`xenonApi.ts:42`). The `body` field on the error is the parsed JSON object — this is what `errorFromResponseBody` in OrderTab reads to surface `reason_code`-driven copy.
7. After IB success, the route checks `initialStatus`. `Cancelled / ApiCancelled / Inactive / Unknown` get re-mapped to **502 `UPSTREAM_ERROR`** (`:198–221`) — IB's "silent rejection" pattern.
8. Fires a best-effort `POST /orders/refresh` (10 s timeout) so the UI's next `/orders` GET sees the new working order.

### 3.3 FastAPI `POST /orders/place` — `src/xenon/api/server.py:2567`

The route handler delegates to `_orders_place_from_body(body)` (`:2745`) after a `require_mode_verified` check when the broker is IB. The internal helper exists so other server-side callers (combo wizard, cancel-then-place) can run the same pipeline — **but in-process callers skip the route's `Depends`**, which is exactly the bypass class that has burned the repo twice (see §13.1).

The validation pipeline runs in this **strict order** (any failure short-circuits with a structured JSON response):

1. **Body schema** — `await request.json()` is implicit; downstream pydantic models (`PreflightRequest`, `ComboPreflightRequest`) raise `ValidationError` and emit `INVALID_ORDER_BODY`.
2. **Account scope** — `_resolve_scope_kwargs()` (`:2577`) reads `app.state.{trading_mode, account, broker}` via `resolve_from_app_state` (`account_scope.py:67`). Falls back to `legacy_unknown` when lifespan didn't run (test mode).
3. **Read-only broker** — if `app.state.broker != "IB"`, returns 403 `READ_ONLY_BROKER` immediately (`:2745–2766`). Catches Futu accidents.
4. **Regime gate** (`_run_regime_gate(body)` at `:2638`):
   - Disabled when `XENON_REGIME_GATE_DISABLED=1` or in test mode without `XENON_REGIME_GATE_IN_TESTS=1` (`:2652`).
   - **Risk-reducing-exit bypass** — `_is_regime_gate_risk_reducing_exit(body)` (`:2148`) returns true for stock SELL unconditionally; for option SELL, requires a portfolio-backed long at the same expiry/strike/right; for combo SELL, requires `combo_close_covered_by_portfolio` (every leg has an opposite-direction inverse with sufficient contracts). Fail-closed on stale (>5 min open / >30 min closed) or missing portfolio snapshot (`:2169`).
   - Build `gate_req` from body (single-leg → `PreflightRequest`; combo → `ComboPreflightRequest` with all legs).
   - Resolve scope-aware `RegimeState` and bankroll: `await get_regime_state_for_scope(scope)` and `await _resolve_regime_bankroll_usd(scope)` (`:2677–2679`).
   - Bankroll precedence: `XENON_REGIME_BANKROLL_USD_OVERRIDE` env → `account_snapshots.net_liquidation` for scope (`:2619–2632`) → `$10_000` failsafe (`regime_gate.py:_BANKROLL_FAILSAFE_USD`). Tight on purpose so unknown-NAV orders trip the cap.
   - `evaluate_order_gate(gate_req, state, bankroll_usd, net_price)` returns `OrderGateOutcome` (`regime_gate.py:333`). The decision flow (`regime_gate.py:_TIER_ACTION_MAP`):
     - PANIC / TIER_1 + non-hedge → BLOCK. PANIC / TIER_1 + hedge → OK (hedges bypass tier blocks).
     - TIER_2 → THROTTLE with `max_loss_cap = 0.0125 × bankroll`, `cover_ratio = 1.25` (so Gate 4 demands 125 shares per short call).
     - EDR / UNKNOWN → THROTTLE with `cover_ratio = 1.0`.
     - NORMAL → OK.
   - On BLOCK without valid `body.override == True && body.override_reason ≥ 10 chars` → **JSONResponse 409** with `reason_code: "REGIME_BLOCK"`, `override_required: True`, `binding_tier`, `vcg_tier`, `cri_tier`, `binding_side` (`:2686–2699`).
   - On `outcome.exceeds_throttle_cap` (THROTTLE + max_loss > cap) → **JSONResponse 422** with `reason_code: "REGIME_RESIZE_REQUIRED"`, `max_loss_usd`, `max_loss_cap_usd`, `cover_ratio` (`:2702–2719`).
   - **Critical detail:** every regime response uses `JSONResponse(content={...})` rather than `HTTPException(detail={...})`. This is the lesson of feedback memory `[HTTPException dict detail breaks toast]` — `HTTPException` wraps the dict under `body.detail.reason_code`, not `body.reason_code`, which breaks `getReasonToast` lookup.
5. **Preflight (Gate 4)** — `_run_preflight(body, cover_ratio=cover_ratio_for_preflight)` (`:2226`) using whatever `cover_ratio` the regime gate plumbed (1.0 unless TIER_2):
   - For combo with no uncovered shorts or SELL envelope: pure call to `preflight.evaluate_combo(req, PortfolioView())` (skip portfolio).
   - For BUY single-leg: same — `evaluate(req, PortfolioView())` (universe + INDEX_HAS_NO_STOCK only).
   - For SELL single-leg or combo with uncovered shorts: load `PortfolioView` via `_load_portfolio_view()`, fail-closed on stale (`_portfolio_snapshot_stale_response`), apply the full ladder (universe → stock-cover → option-cover with vertical-spread accounting → ETF stock-cover with `cover_ratio` threshold).
   - Returns a `Verdict(accept, reason_code, reason_detail)`. Blocked → JSON 400 with `detail`, `reason_code`, `reason_detail` (`:2784–2791`).
6. **Quote gate** (`_validate_non_combo_quote(body)` at `:2463`) — only for non-combo. Combo quote validation is currently a no-op (`:2812`); the BAG net price is sanity-checked by IB plus the on-form `netPrices` calc but no server-side `quote_guard.check_payload` runs.
   - Market hours: `quote_guard.check_market_hours` rejects equity-option orders outside 09:30–16:00 ET (returns `OPTION_MARKET_CLOSED`).
   - Token branch: if `body.quote_token` truthy, `quote_tokens.verify` either populates a `QuotePayload` or raises `QuoteTokenExpired` (silently downgrade to fresh-quote path) or `QuoteTokenInvalid` (return 400 `STALE_QUOTE`). Currently dead in production: UI never sets `quote_token` (commit `654d72d2`, see §13.3).
   - Fresh-quote path: if no payload yet, `_fetch_order_quote_snapshot(body)` queries IB for live bid/ask; converts to `QuotePayload`.
   - `quote_guard.check_payload(...)` runs:
     - Contract match: `payload.ticker == body.symbol && payload.con_id == expected_con_id` → else `QUOTE_CONTRACT_MISMATCH`.
     - Crossed/zero quote → `QUOTE_UNAVAILABLE`.
     - Tick grid: `_lookup_min_tick_via_pool` returns 0.01 for every contract **by design** — see `server.py:2271–2300` and feedback memory `[Tick stub is by-design]`. Off-tick → `LIMIT_OFF_TICK`. Real tick rule is enforced by IB; code 110 from IB re-maps to `LIMIT_OFF_TICK` on the way back.
     - Price band: BUY cap = `min(ask × 1.05, ask + 2 × min_tick)`; SELL floor = `max(bid × 0.95, bid - 2 × min_tick)`. Out-of-band → `LIMIT_OUT_OF_BAND` unless `body.acknowledge_limit_override === true` (in which case the order proceeds and a `PREFLIGHT_ACK_LIMIT` `order_event` is recorded — `:2864`).
7. **Atomic reservation** (`orders_store.reserve_attempt` at `:2836`):
   - Required: `body.client_attempt_id` (else 400 with `client_attempt_id is required`).
   - Builds a `RequestRow` (Postgres-mapped pydantic model). `security_type` is `STK | OPT | BAG`.
   - `pg_insert(order_submissions).values(...).on_conflict_do_nothing(constraint="uq_order_sub_user_attempt").returning(submission_id)` (`orders_store.py:110–133`).
   - **If `override_audit` is set** (`_build_override_audit` at `:2724` — only when the gate would have BLOCK'd but a valid override was supplied): `INSERT regime_overrides` in the **same transaction** (`orders_store.py:135–153`). The composite FK on `regime_overrides` is `DEFERRABLE INITIALLY DEFERRED`, so the parent + child commit atomically.
   - Three outcomes:
     - `winner` — fresh row inserted, state `PENDING`, `submission_id` returned to caller.
     - `duplicate` — conflict hit but existing row is still in flight (i.e. not in `_TERMINAL_STATES`); 200 echoed with `duplicate_of: <ib_order_id>`.
     - `terminal` — conflict hit and the existing row is in `REJECTED|CANCELLED|FAILED`; 409 with `reason_code: ATTEMPT_ID_TERMINAL`. Forces the UI to mint a new `client_attempt_id` rather than retry.
8. **Subprocess dispatch:**
   - Test mode short-circuits with synthetic `orderId/permId` from `_next_test_order_ids` and immediately calls `mark_submitted` (`:2867–2883`).
   - Real mode: `await _run_ib_script_with_recovery("xenon-ib-place-order", ["--json", json.dumps(body)], timeout=15)`. Subprocess executes Layer 3.
   - On subprocess failure (no JSON, non-zero exit): mark `FAILED` with `SUBPROCESS_ERROR`, raise 502 (`:2887–2895`).
   - On `result.data["status"] == "error"`: classify IB code:
     - `code == "110"` → `LIMIT_OFF_TICK` (with structured warning log including symbol, limit_price, ib_message — `ib_place_order.py:152–167`).
     - else → generic `IB_REJECT`.
     - `mark_terminal(state="REJECTED", reason_code=...)`, `record_event("IB_REJECT", {ib_code, ib_message})`, raise 502 (`:2914–2933`).
   - On success: `mark_submitted(submission_id, ib_order_id, perm_id, placing_client_id)` (`orders_store.py:485`). State transitions `PENDING → WORKING`. **Same transaction** also UPDATEs the `regime_overrides` row with `perm_id` and `ib_order_id` if it exists — back-fills audit identity (`orders_store.py:519–526`). Returns the `result.data` dict — the Next.js route then re-keys this into its response.

### 3.4 Layer 3 — `xenon-ib-place-order` (subprocess)

1. Parses `--json '...'` (`ib_place_order.py:204–215`) into `params`.
2. Validates required fields again (defense in depth).
3. `IBClient().connect(host="127.0.0.1", port=4002, client_id="auto", timeout=10)` — `client_id="auto"` allocates from range 20–49.
4. Builds the `Contract`:
   - Stock → `Stock(symbol, "SMART", "USD")`.
   - Option → `Option(symbol, expiry, strike, right, "SMART", "USD")`.
   - Combo → loop over legs to build `Option` objects, then a `Contract(secType="BAG", ...)` with `comboLegs`.
5. `qualified = client.qualify_contracts(contract)` — IB's `qualifyContractsAsync` rolled into the sync wrapper (see `[ib_async in FastAPI]` memory: never `asyncio.to_thread` around ib_async sync calls; the sync wrapper exists in IBClient's own thread).
6. Subscribe to error events: `client._ib.errorEvent += _on_error` filters informational codes (2104/2106/2108/2158/10358) and stashes the rest in `ib_errors`.
7. Build `LimitOrder(action, totalQuantity=quantity, lmtPrice=limit_price, tif=tif, outsideRth=False)`. For combo, attach `smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]`.
8. `trade = client.place_order(contract, order)` — wraps `ib_async.IB.placeOrder`.
9. **Sleep 2 s (single-leg) / 5 s (combo)** to give IB time to ack. Combo leg routing + risk checks take longer.
10. Read `trade.order.orderId`, `trade.order.permId`, `trade.orderStatus.status`.

- **Note:** `permId == 0` until `openOrder` ack arrives (see `[ib_async permId=0 race]` memory). The 2 s/5 s sleep is a coarse-grained workaround. Trade rehydrate / BAG aggregation flows that key by permId have to poll until non-zero.

11. If any IB error event was captured: emit a structured warning (esp. for code 110), return `{"status":"error","code":code,"message":...}`.
12. Else return `{"status":"ok","orderId":...,"permId":...,"tif":...,"initialStatus":...,"message":...}`.

### 3.5 Layer 4 — IB Gateway exchange

Per ib_async's [`Trade` lifecycle](https://ib-async.readthedocs.io):

1. `placeOrder(contract, order)` enqueues an outgoing OPlaceOrder message.
2. IB returns `openOrder(orderId, contract, order, orderState)` — at which point `Trade.order.permId` is finally populated.
3. `orderStatus(status="PreSubmitted"|"Submitted")` follows.
4. On execution: `execDetails(reqId, contract, execution)` arrives with a unique `execId`.
5. `commissionReport(commissionReport)` arrives **separately**, possibly minutes later (esp. for BAG). The activity poller (Layer 2b background task) handles this — see §10.3.
6. `orderStatus(status="Filled")` and `Trade.fills` populates.
7. Errors via `errorEvent` (in IBClient): the place subprocess only listens for the duration of its 2/5 s wait window; the activity poller picks up everything else asynchronously.

### 3.6 Persistence path on success

Back in FastAPI:

- `mark_submitted` sets `state = WORKING`, persists `ib_order_id`, `perm_id`, `placing_client_id`.
- `regime_overrides.{perm_id, ib_order_id}` are back-filled in the same transaction (audit row joins to the actual IB IDs).
- The Next.js route then fires `POST /orders/refresh` (best-effort) which triggers IB pool to pull the latest open orders into the orders cache. The next `GET /orders` from the UI shows the working order.

---

## 4. Validation Pipeline — Strict Stage Order

The 8 stages in `_orders_place_from_body` (`server.py:2745`):

```
            ┌──────────────────────────────────────┐
   POST   ──▶ 1. Body schema (pydantic)            │
   body     │     fail → 400 INVALID_ORDER_BODY    │
            └────────────────┬─────────────────────┘
                             ▼
            ┌──────────────────────────────────────┐
            │ 2. Account scope                     │
            │    resolve_from_app_state            │
            │    fail / non-IB → 403 READ_ONLY_BROKER
            └────────────────┬─────────────────────┘
                             ▼
            ┌──────────────────────────────────────┐
            │ 3. Read-only broker (Futu)           │
            │    fail → 403 READ_ONLY_BROKER       │
            └────────────────┬─────────────────────┘
                             ▼
            ┌──────────────────────────────────────┐
            │ 4. Regime gate                       │
            │    risk-reducing-exit?  → bypass     │
            │    BLOCK no override   → 409         │
            │    THROTTLE > cap      → 422         │
            │    OK / THROTTLE in cap → cover_ratio
            │      threaded into preflight         │
            └────────────────┬─────────────────────┘
                             ▼
            ┌──────────────────────────────────────┐
            │ 5. Preflight (Gate 4) — pure         │
            │    universe → stock cover → option   │
            │    cover (incl vertical spread) →    │
            │    ETF share-cover with cover_ratio  │
            │    fail → 400 reason_code            │
            └────────────────┬─────────────────────┘
                             ▼
            ┌──────────────────────────────────────┐
            │ 6. Quote gate (single-leg only)      │
            │    market hours → contract match →   │
            │    sanity → tick grid → price band   │
            │    fail → 400 reason_code            │
            │    (combo: no server quote check)    │
            └────────────────┬─────────────────────┘
                             ▼
            ┌──────────────────────────────────────┐
            │ 7. reserve_attempt (atomic)          │
            │    ON CONFLICT DO NOTHING            │
            │    + regime_overrides INSERT (txn)   │
            │    winner/duplicate/terminal         │
            └────────────────┬─────────────────────┘
                             ▼
            ┌──────────────────────────────────────┐
            │ 8. Subprocess dispatch               │
            │    xenon-ib-place-order              │
            │    success → mark_submitted (WORKING)│
            │    error → mark_terminal             │
            └──────────────────────────────────────┘
```

**Why this order matters:**

- **Regime gate before preflight** — when TIER_2 is binding, `cover_ratio = 1.25` must reach Gate 4 so the share-cover threshold tightens to 125 shares per short call.
- **Regime gate before reserve_attempt** — a regime BLOCK should not consume the `client_attempt_id`. The atomic INSERT happens only after we've decided to actually submit.
- **Preflight before quote gate** — preflight is cheaper (no IB round-trip) and rejects more often. Skipping it saves on quote calls during pathological flows.
- **Reserve before subprocess** — idempotency happens _before_ we fire and forget into IB. If the subprocess hangs, the row is `PENDING`. **Caveat:** the `PENDING_TIMEOUT` reaper (`single_leg_rehydrate.py:31` — `PENDING_TIMEOUT_SECONDS = 60`) only runs once on FastAPI lifespan boot. There is no continuous sweep — see §13.11.

---

## 5. Frontend Detail — OrderTab Component Tree

```
OrderTab (web/components/ticker-detail/OrderTab.tsx)
├── ExistingOrderRow[]                          (line :136)
│   └── uses OrderActionsContext.requestCancel  (web/lib/OrderActionsContext.tsx:205)
├── ModifyOrderModal                            (web/components/ModifyOrderModal.tsx)
│   └── POSTs /api/orders/modify
│   └── parseRegimeGateResponse (regime_gate.ts:56) on 4xx
├── NewOrderForm                                (line :347)
│   ├── checkNakedShortRisk reactive memo       (line :420)
│   ├── OrderConfirmSummary                     (web/lib/order/components/...)
│   ├── handlePlace                             (line :436)
│   │   ├── buildSingleLegOrderPayload           (line :286)
│   │   ├── checkNakedShortRisk (defense-in-depth) (line :458)
│   │   ├── attemptId.markSubmitted              (uses useClientAttemptId)
│   │   ├── fetch /api/orders/place              (line :473)
│   │   ├── parseRegimeGateResponse              (line :478)
│   │   │   ├── 409 REGIME_BLOCK  → setRegimePrompt(kind:"block")
│   │   │   ├── 422 REGIME_RESIZE → setRegimePrompt(kind:"resize")
│   │   │   └── other            → errorFromResponseBody
│   │   └── on ok: setSuccess + onOrderPlaced + attemptId.markTerminal
│   ├── retryRegimeOrder                         (line :527)
│   ├── handleRegimeOverride                     (line :580) — buildRegimeOverrideFields
│   └── handleRegimeResize                       (line :591) — suggestResizeQuantity (mutates qty)
└── ComboOrderForm                              (line :788)
    ├── legsWithActions memo                    (line :819) — LONG→BUY, SHORT→SELL (never flip)
    ├── netPrices memo                          (line :843) — natural market BAG bid/ask, IB-reversal aware
    ├── nakedShortWarning memo                  (line :891)
    ├── handlePlace                             (line :914)
    │   └── posts {type: "combo", legs, ...}
    └── retryRegimeOrder / handleRegimeOverride / handleRegimeResize
```

### 5.1 Combo net-price calculation — sign convention

The combo `netPrices` calc (`OrderTab.tsx:843–880`) is the most subtle piece of the form. Its job is to compute net BID and net ASK for the spread _as the user will pay_ — accounting for IB's leg-action reversal when the BAG envelope action is SELL.

```typescript
// OrderTab.tsx:858–873
const effectivelySelling = (action === "SELL") === (leg.direction === "LONG");

if (effectivelySelling) {
  netBid += lp.bid;
  netAsk += lp.ask;
} else {
  netBid -= lp.ask;
  netAsk -= lp.bid;
}
```

The XOR-style boolean means: a LONG leg in a BUY combo is "buying it" (effectivelySelling=false → pay ASK, receive BID at close); a SHORT leg in a BUY combo is "writing it" (effectivelySelling=true → receive BID for the short, pay ASK if reversed).

This implements the cross-fields rule from `web/CLAUDE.md`:

> To BUY combo: pay ASK on BUY legs, receive BID on SELL legs

The wrong (mid-mid) implementation would use `sign × bid` and `sign × ask` — it would yield identical bid and ask, masking spread. See `web/CLAUDE.md` lines 36–54 for the worked example.

### 5.2 IB BAG leg convention

`legsWithActions` (`OrderTab.tsx:819–832`) preserves structure-level actions:

```typescript
const legAction: "BUY" | "SELL" = leg.direction === "LONG" ? "BUY" : "SELL";
```

This is the LONG→BUY / SHORT→SELL invariant from `src/xenon/CLAUDE.md` Combo / BAG Order Guardrails. **The BAG envelope `Order.action` (BUY for opens, SELL for closes) controls open vs close; leg actions encode the spread structure.** Flipping leg actions on close causes IB error 201 (double-reversal). PR #46 (incident #8) is the canonical reproduction.

Note that `_max_loss_combo` in `regime_gate.py:232` branches on `net_price` sign (debit positive, credit negative) — **not** on `order.action` — for exactly this reason. A SELL BAG envelope in a closing flow is still a _credit-receiving_ close on a credit spread; the net price tells the truth.

### 5.3 Regime block / resize UX

When the place fetch returns 409/422, `parseRegimeGateResponse` (`web/lib/order/regimeGate.ts:56`) clones the response and reads body:

```typescript
if (res.status === 409 && reasonCode === "REGIME_BLOCK") {
  return { kind: "block", payload: obj as RegimeBlockResponse };
}
if (res.status === 422 && reasonCode === "REGIME_RESIZE_REQUIRED") {
  return { kind: "resize", payload: obj as RegimeResizeResponse };
}
```

`OrderTab` opens `RegimeBlockModal` with the parsed payload. When the user submits an override reason ≥ 10 chars (validated by `isRegimeOverrideReasonValid` at `regime_gate.ts:89`), `handleRegimeOverride` calls `retryRegimeOrder` with `{...originalBody, ...buildRegimeOverrideFields(reason)}`. **The `client_attempt_id` is reused** — the override path proves intent on the _same_ idempotency slot, so a second-attempt regime block doesn't create a parallel WORKING row.

For resize: `suggestResizeQuantity(payload, currentQuantity)` (`regime_gate.ts:105`) computes the largest quantity that stays under the cap by linear scaling (`floor(currentQty × cap / max_loss)`). The user accepts/edits this in the modal, and `handleRegimeResize` re-POSTs with the trimmed quantity (also reusing `client_attempt_id`).

**Implication:** the same `client_attempt_id` may run through Stage 4 multiple times before reaching Stage 7. This is intentional — Stage 4 is a pure decision; Stage 7 is the atomic commit. But this also means `_run_regime_gate` must be idempotent, which it is (no side effects until `reserve_attempt`).

---

## 6. Regime Gate Detail

### 6.1 Tier ladder

`regime_gate.py:69–76`:

```python
_TIER_ACTION_MAP: dict[TierLabel, tuple[GateDecision, Optional[float], Optional[float]]] = {
    "PANIC":   (BLOCK,    None,            None),
    "TIER_1":  (BLOCK,    None,            None),
    "TIER_2":  (THROTTLE, 0.0125,          1.25),
    "EDR":     (THROTTLE, 0.0125,          1.0),
    "UNKNOWN": (THROTTLE, 0.0125,          1.0),
    "NORMAL":  (OK,       None,            None),
}
```

`binding_tier = worst-of(vcg_tier, cri_tier)`, set in `RegimeState`. The ladder is `NORMAL < EDR < TIER_2 < TIER_1 < PANIC`, plus `UNKNOWN` (which is treated as moderately restrictive — better than `NORMAL`, worse than EDR's logic).

### 6.2 Hedge classification

`_is_hedge` (`regime_gate.py:138`):

| Type                 | Condition                                                                                                                                                       |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Single-leg long PUT  | `ticker ∈ {HYG, JNK, LQD, SPX, SPY}` and `action == BUY`                                                                                                        |
| Single-leg long CALL | `ticker == VIX` and `action == BUY`                                                                                                                             |
| Two-leg vertical     | All same expiry; one BUY + one SELL; debit only (`net_price > 0`); strike geometry: long puts `buy.strike > sell.strike`; long calls `buy.strike < sell.strike` |

The strike-geometry check (`regime_gate.py:195–198`) is what distinguishes a _debit_ put spread (hedge: long higher strike, short lower) from a _credit_ put spread (write risk: long lower, short higher). The credit case is rejected: hedges must be debit.

The **`net_price > 0` requirement** on combos (`regime_gate.py:170–171`) is also a hedge filter — a credit BAG envelope on SPY, even with hedge-shaped legs, is _writing_ protection, not buying it.

### 6.3 Risk-reducing-exit bypass

`_is_regime_gate_risk_reducing_exit(body)` (`server.py:2148`) — bypasses the gate entirely for genuine de-risking:

```python
if action != "SELL":           # only SELLs reduce exposure
    return False
if body.get("type") == "stock":
    return True                # naked-short audit catches the rest
# load portfolio
if combo:
    return preflight.combo_close_covered_by_portfolio(combo_req, portfolio)
if option:
    return _portfolio_has_matching_long_option(portfolio, ticker, expiry, strike, right, quantity)
```

**Fail-closed when portfolio is stale or missing** (`:2167–2170`). The reasoning is in the docstring: a 30-min-old snapshot during a fast-moving panic could "prove" a long that's already been closed, and that would let through a naked short under the bypass.

`combo_close_covered_by_portfolio` (`preflight.py:155`) requires:

1. Single expiry across all legs (no calendar spreads).
2. Every leg has an opposite-direction inverse position with sufficient contracts (aggregated across positions of the same `ticker × expiry`).

### 6.4 Modify gate

`_run_modify_regime_gate` (`server.py:3120`):

- Pure price modify (no qty change) — skip gate.
- Qty decrease — skip gate (reducing risk).
- Qty increase: build a synthetic `PreflightRequest` for the **delta** quantity using `body.newPrice` if provided (avoids the `[modify gate uses stale price]` regression), evaluate gate against delta. BLOCK → 409, THROTTLE > cap → 422.
- BAG qty increase: order_submissions does not persist combo legs, so a synthetic delta can't be reconstructed. At NORMAL: skip (no gate). At any restrictive tier: 409 with `modify_sec_type: BAG`, requiring user to cancel + replace through the gated `/orders/place` path (`:3161–3194`).
- The gate runs **before** `apply_modify` (the monotonic sequence advance) — so a regime-rejected modify does not consume `modify_sequence`.
- Every modify-block response includes `applied_sequence: <current>` so the client counter can sync without falling into a `MODIFY_STALE` retry loop (see `src/xenon/api/CLAUDE.md` Cancel/Modify rule 7).

### 6.5 Override completion

When the gate would BLOCK but a valid override is supplied, the order proceeds. `_build_override_audit` (`server.py:2724`) packs the audit dict; `reserve_attempt` writes it to `regime_overrides` in the same transaction as `order_submissions`. After IB success, `mark_submitted` UPDATEs the same `regime_overrides` row with `perm_id` and `ib_order_id` (`orders_store.py:519–526`). The composite FK is `DEFERRABLE INITIALLY DEFERRED`, so on a winner-INSERT the parent row is visible at COMMIT and the child FK validates.

> **Open follow-up — C-2.3:** the modify path currently does **not** insert a `regime_overrides` row when an override flag is supplied (the override fields are not piped through `apply_modify`/`apply_modify_by_perm_id`). For now, modify-block override is `override_supported: false` in the response (`server.py:3239`).

---

## 7. Quote Gate Detail (and the STALE_QUOTE Mystery)

`quote_guard.check_payload` (`quote_guard.py:47`) does **not** check freshness. The reasoning is that the freshness window only matters when verifying a UI-minted token, and that path is currently dead (see §13.3). The `check()` wrapper (`:105`) does the token verify with `MAX_AGE_RTH_MS = 500` and falls into `check_payload` after.

The `STALE_QUOTE` reason code currently has only three emitters in the entire backend:

- `quote_guard.py:120` — `check()` raises `QuoteTokenExpired`.
- `quote_guard.py:124` — `check()` raises `QuoteTokenInvalid`.
- `server.py:2489` — `_validate_non_combo_quote` catches `QuoteTokenInvalid` from a body-supplied token.

Of these, only `server.py:2489` is reachable from the live place path. It fires when `body.get("quote_token")` is truthy and the token signature/format is invalid. **Production UI does not set `quote_token` since commit `654d72d2` (2026-04-25)** — `web/lib/order/placeOrderContract.ts:35` only attaches it if present in the input body, and no UI caller populates it. So `STALE_QUOTE` is structurally unreachable in current code. (See §13.3 for the historical context — this surface has been re-shipped and reverted twice.)

### 7.1 The 0.01 tick stub — by design

`_lookup_min_tick_via_pool` (`server.py:2271`) returns `Decimal("0.01")` for every contract. This is a deliberate approximation:

- Replicating IB's tick-rule table locally is fragile (Reg NMS Rule 612 sub-penny carve-outs, OPRA penny pilot rosters, exchange market rules).
- Reaching into IB synchronously (`reqContractDetailsAsync`) requires `ib_async` calls under sync wrappers — and feedback memory `[ib_async in FastAPI]` says: never `asyncio.to_thread` around sync calls. The right path is plumbing `reqContractDetailsAsync` through an async `quote_guard`, which is a wider refactor.
- Most US equities and most options at our prices have $0.01 ticks anyway. Edge instruments hit IB's rejection (code 110), and `_orders_place_from_body` re-maps that to `LIMIT_OFF_TICK` so the user sees a clean error and the IB code+message land in `order_events` for telemetry.

The structured warning log on tick rejections (`ib_place_order.py:152–167`) is the trip-wire for re-evaluating this stub — if the rate climbs, the wider refactor is justified.

---

## 8. IB Subprocess Layer

### 8.1 Client ID allocation

| Range          | Owner              | Purpose                                                    |
| -------------- | ------------------ | ---------------------------------------------------------- |
| 0–9            | `ib_pool.py`       | Persistent connections (sync, orders, data) for read paths |
| 20–49          | `client_id="auto"` | On-demand subprocesses (place, manage, quote, reconcile)   |
| (other ranges) | special-purpose    | see `docs/architecture/api-infrastructure.md`              |

This separation matters because:

- **Pool clients (0–9) can SEE all open orders via `reqAllOpenOrders` but cannot cancel/modify them** (IB returns Error 10147 on cancel, Error 103 on modify).
- Cancel and modify subprocesses must **reconnect as the original `clientId` that placed the order** (`ib_order_manage.py` reads `trade.order.clientId` from the open-order snapshot before executing). This is the lesson encoded in `src/xenon/api/CLAUDE.md` Cancel/Modify rule 1.

### 8.2 BAG construction

`ib_place_order.py:47–80`:

```python
options = []
for leg in legs_data:
    opt = Option(symbol, leg["expiry"], float(leg["strike"]), leg["right"], "SMART", "USD")
    options.append(opt)
qualified = client.qualify_contracts(*options)

combo = Contract()
combo.symbol = symbol
combo.secType = "BAG"
combo.currency = "USD"
combo.exchange = "SMART"
combo_legs = []
for i, leg in enumerate(legs_data):
    cl = ComboLeg()
    cl.conId = qualified[i].conId  # qualified per-leg conId
    cl.ratio = int(leg.get("ratio", 1))
    cl.action = leg["action"].upper()  # the structure encoding
    cl.exchange = "SMART"
    combo_legs.append(cl)
combo.comboLegs = combo_legs
```

And the order:

```python
order = LimitOrder(action=action, totalQuantity=quantity, lmtPrice=limit_price, tif=tif, outsideRth=False)
order.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]
```

- `Order.action` controls open vs close (open: BUY, close: SELL).
- `ComboLeg.action` encodes structure (LONG → BUY, SHORT → SELL — never flipped).
- `ratio` defaults to 1 — the combo-wizard supports asymmetric ratios for ratio spreads / 1x2s.
- `NonGuaranteed=1` lets IB route legs separately; trade-off: faster fills, no leg-coordination guarantee.

### 8.3 The permId race

`ib_async` exposes `Trade.order.permId == 0` until the `openOrder` ack arrives from IB (memory `[ib_async permId=0 race]`). The 2 s (single-leg) / 5 s (combo) sleep in `place_order` is a coarse-grained workaround — usually enough but not guaranteed. Downstream code that keys by `perm_id` (BAG aggregation, single-leg rehydrate) **must poll until non-zero** rather than read once.

The `mark_submitted` call passes whatever `permId` ib_place_order returned. If that's still 0 (rare — would mean IB hadn't acked in 5 s), the row's `perm_id` is `"0"`. The activity poller's `register_from_snapshot` will reconcile this on the next tick if a real permId shows up later, with the UUID-row precedence rule (see §10.2).

### 8.4 The `reqTickersAsync` hang on index options

Memory `[ib_async in FastAPI]`: `reqTickersAsync` hangs on index options (SPX, NDX, RUT). The fresh-quote branch of `_validate_non_combo_quote` uses a different path (`_fetch_order_quote_snapshot`) that uses pool sync wrappers — so this doesn't bite the place flow directly, but is relevant for any future `quote_guard` async refactor.

---

## 9. Postgres Persistence

### 9.1 Lifecycle of an `order_submissions` row

```
                        reserve_attempt → state=PENDING
                                  │
                                  │   subprocess success
                                  ▼
                        mark_submitted → state=WORKING
                              ib_order_id, perm_id set
                                  │
                ┌─────────────────┼─────────────────┐
                ▼                 ▼                 ▼
        IB error       successful fill       /orders/cancel
        mark_terminal  mark_terminal         _mark_submission_cancelled
        REJECTED       FILLED                CANCELLED
        IB_REJECT      filled_qty,           USER_CANCEL
        LIMIT_OFF_TICK avg_fill_price set
        ATTEMPT_ID_TERMINAL
```

`_TERMINAL_STATES = {"REJECTED", "CANCELLED", "FAILED"}` (`orders_store.py:63`). FILLED is not in this set — it's a happy terminal but the row remains in `WORKING` semantics until the activity poller writes the fills and `single_leg_rehydrate` / `trade_aggregator` derives the closed state. (Some flows do `mark_terminal(state="FILLED")` directly — the set is for idempotency-key collision detection in `reserve_attempt`.)

### 9.2 Composite FK semantics (regime_overrides)

Schema (`schema.py:404–448`):

```python
ForeignKeyConstraint(
    ["submission_id", "broker", "account_env", "broker_account"],
    [".order_submissions.submission_id",
     ".order_submissions.broker",
     ".order_submissions.account_env",
     ".order_submissions.broker_account"],
    name="fk_regime_overrides_submission_scope",
    deferrable=True,
    initially="DEFERRED",
)
```

Why composite + deferrable:

- **Composite** (the original lesson, ISSUE-5) — without scope columns on the FK, an override row could reference a submission in a different account, breaking paper/live isolation. The composite FK requires scope-match.
- **Deferrable initially deferred** — `reserve_attempt` does INSERT submission, INSERT regime_overrides, then COMMIT in a single transaction. The override INSERT references the submission_id that was just generated. Without DEFERRED, the FK would check at INSERT time — depending on Postgres internals this could pass anyway for a same-statement insert, but DEFERRED makes it explicit that validation is at COMMIT.
- **Matching UNIQUE on the parent** — `uq_order_sub_submission_scope(submission_id, broker, account_env, broker_account)` (`schema.py:238–244`). Logically redundant under the PK, but Postgres requires the FK target to have an explicit UNIQUE/PK constraint covering all 4 columns.

Test that locks this in: `scripts/tests/test_regime_overrides_audit.py`. Note PR #78 caught a fixture bug here — `broker="ib"` (lowercase) violated `CHECK ck_order_sub_broker_ib_only` (which requires `'IB'` literal), so the test had been latent until coverage drift caught it.

### 9.3 Bankroll source

`account_snapshots.net_liquidation` is the production bankroll input. `_resolve_regime_bankroll_usd` (`server.py:2604`) reads it via `get_latest_net_liquidation_for_scope` (`src/xenon/db/queries/portfolio.py:104`):

```sql
SELECT net_liquidation
FROM xenon.account_snapshots
WHERE broker = :broker AND account_env = :env AND broker_account = :acct
  AND net_liquidation IS NOT NULL
ORDER BY snapshot_at DESC LIMIT 1
```

When this returns NULL (cold boot, before first sync), the failsafe is `$10_000` (`regime_gate.py:_BANKROLL_FAILSAFE_USD`). Tight on purpose — at TIER_2 the per-order cap becomes `0.0125 × 10_000 = $125`, so any real-money order will trigger 422 and force the user to acknowledge before proceeding. Better than silently sizing against $100k.

### 9.4 Activity mirror tables

`order_fills` (`schema.py:266–306`):

- PK is IB's `exec_id`. Idempotency is structural (ON CONFLICT DO NOTHING).
- `submission_id` is FK to `order_submissions` but **nullable** (a snapshot-mirrored fill from a TWS-placed order has no submission row at first; `register_from_snapshot` creates a `snapshot-*` row to fill the gap, but if it's a true legacy/orphan, the CHECK `ck_fills_source_present` requires either `submission_id`, `combo_attempt_id`, or `metadata.legacy_source`).
- `commission` defaults to 0; `update_fill_commission` (`orders_store.py:631`) patches it when the late-arriving CommissionReport message arrives. Three test cases lock in: first-tick zero insert, second-tick non-zero update, idempotent zero-zero no-op.

### 9.5 Outbox events

`emit_outbox_in_txn` (`src/xenon/db/events.py:42–51`) writes to `events.outbox` inside the caller's open transaction. Channels:

- `fill.recorded` — emitted by `record_fill` (`orders_store.py:605`) on first fill insert.
- `fill.commission_updated` — emitted by `update_fill_commission` when `commission` or `realized_pnl` changes.

These are LISTEN/NOTIFY channels for reactive services (e.g. trade aggregator self-heal). Not currently piped to the UI WebSocket — the UI relies on `/orders` polling.

---

## 10. Modify, Cancel, and the TWS-Cancel Mirroring Gap

### 10.1 Single-leg modify — happy path

1. `ModifyOrderModal` (`web/components/ModifyOrderModal.tsx`) collects `newPrice` and/or `newQuantity`, plus the current per-order `modifySequence` counter (incremented client-side per modify).
2. POST `/api/orders/modify` (`web/app/api/orders/modify/route.ts:88`).
3. Single-leg path (no `replaceOrder`): validates `newPrice > 0`, `newQuantity > 0`, then `xenonFetch("/orders/modify", { ... modifySequence })` (`:230`).
4. FastAPI `_orders_modify_from_body` (`server.py:3265`):
   - Require `modifySequence` (else 400 `MODIFY_SEQUENCE_REQUIRED`).
   - Require `orderId` or `permId` (else 400 `ORDER_IDENTIFIER_REQUIRED`).
   - `load_submission_for_modify(order_id_or_perm_id, **scope)` (`orders_store.py:717`).
   - **Run regime gate first** (`_run_modify_regime_gate`, §6.4). If it returns 409/422, the response includes `applied_sequence: <current>` so the UI counter can sync — and `apply_modify` is **not** called, so the sequence is preserved.
   - `apply_modify(order_id, sequence, **scope)` or `apply_modify_by_perm_id` if only `permId` is supplied. This is the monotonic gate: `UPDATE order_submissions SET modify_sequence = :sequence WHERE ib_order_id = :id AND modify_sequence < :sequence RETURNING modify_sequence` (`orders_store.py:218–229`). 0 rows updated → either the row doesn't exist (404 `ORDER_NOT_FOUND`) or the sequence is stale (409 `MODIFY_STALE` with `applied: <current>`).
   - Subprocess `xenon-ib-order-manage modify --order-id ... --new-price ... --new-quantity ...` (15 s timeout).
   - On subprocess failure: DB sequence is **already advanced** — don't roll back (prevents double-apply on retry). Surface 503 with `applied_sequence: <N>` so the client counter stays correct.
   - On `result.data.status == "error"`: classify via `_classify_to_http`:
     - `connection` → 503 IB_CONNECTION
     - `ownership` → 409 OWNERSHIP
     - `ib_reject` → 400 (or 404 for codes 10147/10148) IB_REJECT
   - On success: record MODIFY event, return `{...data, applied_sequence: modify_sequence}`.
5. Next.js route then GETs `/orders` and runs `isModifyConfirmed` (`route.ts:29`) — checks the refreshed open orders and verifies the new price/quantity are present (with `Math.abs(diff) < 0.001` for price). If not confirmed, returns 502 `Modify not confirmed by refreshed orders`.

### 10.2 Combo modify — cancel-then-place (data-loss risk)

`web/app/api/orders/modify/route.ts:105–204` handles the `replaceOrder` branch:

```typescript
for (const cancelTarget of cancelTargets) {
  await xenonFetch("/orders/cancel", { ... });
}
// Cancel-then-place is unavoidable here (IB has no atomic restructure for
// combo legs), but we wrap in try/catch so a place failure surfaces a
// CRITICAL error that names the data-loss situation explicitly.
try {
  result = await xenonFetch("/orders/place", { ... replaceOrder });
} catch (placeErr) {
  return NextResponse.json({
    error: "CRITICAL: Original order cancelled, replacement FAILED. Place a new order manually.",
    detail: { placeError: placeMsg },
    orders,
  }, { status: 502 });
}
```

This is the **C-2.2 backlog item** flagged for follow-up. The narrow fix would be: persist the original combo legs in `order_submissions` (or a sibling table) so a failed replacement could auto-recover by replaying. The wider fix would be a server-side `submit_combo_replacement` route that wraps the cancel+place under a single rollback boundary. Today, on failure: original is cancelled, replacement is gone, user is told to manually re-enter.

### 10.3 Cancel — happy path

1. Either `ExistingOrderRow` button (`OrderTab.tsx:164` → `OrderActionsContext.requestCancel`) or `ModifyOrderModal` cancel.
2. `web/app/api/orders/cancel/route.ts` posts to `xenonFetch("/orders/cancel", { orderId, permId })`, then refresh + GET `/orders`.
3. FastAPI `_orders_cancel_from_body` (`server.py:3021`):
   - Subprocess `xenon-ib-order-manage cancel --order-id ... --perm-id ...`.
   - On `not result.ok`: 503 with `IB_CONNECTION` reason and event row.
   - On `data.status == "error"`: classify as above (10147 → 404).
   - On success: record CANCEL event + `_mark_submission_cancelled` (`server.py:3071`):
     - Looks up `submission_id` by `ib_order_id` then by `perm_id` (`orders_store.lookup_submission_id_by_*`).
     - Calls `mark_terminal(state="CANCELLED", reason_code="USER_CANCEL")`.
4. UI re-renders from refreshed `/orders`. The cancelled order row clears.

### 10.4 The TWS-cancel mirroring gap

If the user cancels an order in **TWS** (not the Xenon UI), the backend is blind. The activity poller (`ib_activity_mirror`) calls `sync_open_orders_to_postgres` which inserts new orders found via `reqAllOpenOrders` and updates drifted rows (`register_from_snapshot`) — but it does **not** transition `WORKING` → `CANCELLED` for orders that disappeared from `get_open_orders()`. Reason (per `src/xenon/api/CLAUDE.md` line 44): naive disappearance-detection misclassifies _fills_ as cancels (an order that fills mid-tick also disappears). The right fix combines disappeared-set ∩ no-fills-in-`order_fills` ∩ idle grace window, but that's not yet implemented.

Operational impact: a TWS-cancelled order stays `WORKING` in `order_submissions` until the next FastAPI restart triggers `single_leg_rehydrate.rehydrate_on_boot`, which drops it from the working set. The UI ends up showing a phantom "open" order until refresh. **This is the longest-standing open gap in the order surface.**

### 10.5 Cancel/modify failure classification

`_classify_to_http(data)` (`server.py:2955`):

| Subprocess `classification`    | HTTP | reason_code         |
| ------------------------------ | ---- | ------------------- |
| `connection`                   | 503  | IB_CONNECTION       |
| `ownership`                    | 409  | OWNERSHIP           |
| `ib_reject` + code 10147/10148 | 404  | IB_REJECT           |
| `ib_reject` (other)            | 400  | IB_REJECT           |
| (missing/unknown)              | 502  | IB_REJECT (generic) |

The full upstream payload (`upstream.code`, `upstream.message`) is preserved in the response `detail` — the lesson of `src/xenon/api/CLAUDE.md` Cancel/Modify rule 5: do not collapse provider failures to generic 500s.

---

## 11. UI Feedback Loop

There is **no live WebSocket push for order events**. The reactive surface is:

1. **Price WebSocket** (`web/lib/usePrices.ts:354,515`) — pushes bid/ask/last for subscribed contracts; drives `netPrices` recomputation and the price strip. Distinct from order events.
2. **Polling `/orders`** — `useIbOrders`-style hooks GET `/api/orders` periodically (and after every place/modify/cancel mutation, the route handlers proactively call `POST /orders/refresh` to warm IB → cache → DB before the next GET).
3. **Imperative state** — `OrderActionsContext` (`web/lib/OrderActionsContext.tsx:75`) tracks `pendingModifies: Map<permId, OpenOrder>` so the UI can show "modifying…" badges between the POST and the next refresh.
4. **Toast copy** — `getReasonToast(code)` (`web/lib/orderReasonCodes.ts:111`) maps `reason_code` → `{ severity, copy }`. The map must stay in parity with the Python `ReasonCode` StrEnum (file-top comment is the canon link).

Reason codes flow through:

```
preflight.py ReasonCode enum  →  HTTP body { reason_code }
         │                              │
         │                              ▼
         │                       errorFromResponseBody(body)
         │                              │
         └─ source of truth ──────────  │
                                        ▼
                              getReasonToast(code).copy
                                        │
                                        ▼
                                 user-facing toast
```

The trap that has burned this twice (memory `[HTTPException dict detail breaks toast]`):

- `raise HTTPException(status_code=409, detail={"reason_code": ...})` produces `{ "detail": { "reason_code": ... } }` — `getReasonToast(body.reason_code)` returns nothing.
- `return JSONResponse(status_code=409, content={"reason_code": ..., "detail": "..."})` produces `{ "reason_code": ..., "detail": "..." }` — `getReasonToast(body.reason_code)` works.
- For new error mappings, **always prefer `JSONResponse(content=...)` over `HTTPException(detail=dict)`**.

---

## 12. Failure Mode Catalog

### 12.1 Naked-short reject (Gate 4)

UI side blocks first via `checkNakedShortRisk`. If the form is somehow submitted (e.g. via a non-OrderTab caller), server-side `preflight.evaluate` returns `INSUFFICIENT_SHARES` / `INDEX_CALL_UNCOVERED` / `ETF_CALL_UNCOVERED`. The **post-sync audit** (`naked_short_audit.py`) is the third layer — it scans every open order after each `ib_sync` and cancels violators automatically.

### 12.2 Regime block + override

Single-leg place in PANIC tier:

- Server returns 409 `REGIME_BLOCK`, `override_required: true`, `override_min_reason_chars: 10`.
- `OrderTab` opens `RegimeBlockModal`.
- User types reason ≥ 10 chars → `handleRegimeOverride` → `retryRegimeOrder({...body, override: true, override_reason})`.
- Same `client_attempt_id` → server gate sees `override_requested && len(reason) >= 10` → proceeds with `override_audit` set → `reserve_attempt` writes both rows in one txn → subprocess → on success, `mark_submitted` back-fills `regime_overrides.{perm_id, ib_order_id}`.
- Audit row in `regime_overrides` is the durable record of "user knowingly bypassed the gate at PANIC".

### 12.3 Tick-off-tick (IB code 110)

- IB returns `errorCode=110` "The price does not conform to the minimum price variation".
- `ib_place_order.py:152–167` emits a structured warning log (`event: "ib_tick_rejection"`, `ib_code: 110`).
- The subprocess returns `{"status":"error","code":110,"message":...}`.
- `_orders_place_from_body` (`server.py:2905`) re-maps to `LIMIT_OFF_TICK` instead of generic `IB_REJECT`.
- `mark_terminal(state="REJECTED", reason_code="LIMIT_OFF_TICK")` + `record_event("IB_REJECT", {ib_code, ib_message})`.
- HTTPException 502 with detail = `ib_message`.
- Next.js passes through (`xenonFetch` preserves body); UI surfaces `getReasonToast("LIMIT_OFF_TICK").copy`.

### 12.4 STALE_QUOTE (currently unreachable)

The structural unreachability per current code (commit `654d72d2`):

- `STALE_QUOTE` is emitted only at `server.py:2489` (token invalid) — not on body without token.
- UI `placeOrderContract.ts:35` only attaches `quote_token` if the input body has it — and no production code path sets `quote_token`.
- Even if a stale token were attached, the gate emits `STALE_QUOTE` only on `QuoteTokenInvalid` (signature/format mismatch), not `QuoteTokenExpired` (which silently downgrades to fresh-quote fetch).

So in production today, the only way to see `STALE_QUOTE` is a forged or signature-mismatched token in a request body — i.e. a test or a manual `curl`.

### 12.5 IB BAG leg double-reversal (code 201)

If a UI bug flips leg actions on a SELL combo close, IB rejects with code 201. Lesson encoded in `web/CLAUDE.md` BAG Leg Convention and `web/lib/order/...` tests. The actual `legsWithActions` memo (`OrderTab.tsx:819`) is the canonical site that prevents this — never invert leg.action based on combo.action.

---

## 13. Architectural Concerns (Honest Assessment)

This section flags pre-existing fragility in the order stack. Not prescriptive — just the things a senior engineer should know about before they think the surface is calm.

### 13.1 The in-process bypass class

FastAPI `Depends` runs only on HTTP entries. In-process callers — `_orders_place_from_body`, `_orders_modify_from_body`, `_orders_cancel_from_body`, `submit_combo` — skip every dep. Two memorialized regressions:

- **#34 quote_token (incident #5)** — disabled-Confirm gate from the new quote-token surface bypassed unit tests because the gate's failure mode was silent.
- **#47 audit gating (incident #6)** — same flow re-shipped a day later by a reviewer who didn't read the `web/CLAUDE.md` warning.
- **#61 (incident #12)** — combo Gate-4 preflight was not reaching in-process callers; fix was to call preflight at the function level rather than relying on Depends.

**Layered guards** (today):

- Edit-time hook: `.claude/hooks/order-path-reminder.sh` — PreToolUse advisory checklist.
- CI: `scripts/checks/no_json_fallback_on_order_path.py` — fails new `data/*.json` reads on order path. Existing legacy reads pinned in `_ALLOWLIST`; intent is to shrink to zero.
- CI: `scripts/checks/order_path_caller_allowlist.py` — fails imports / CLI invocations of `xenon.execution.ib_place_order` outside `(server.py, the module itself, tests, pyproject.toml)`. Locks the bypass.
- Optional pre-commit: `scripts/checks/install-pre-commit.sh` drops a managed hook.

The structural risk: **central enforcement at the route is not enough** — every helper that an in-process caller might use needs the same check inside. The combo Gate-4 fix at the function level (incident #12) is the canonical pattern for this.

### 13.2 The HTTPException-vs-JSONResponse trap (memory)

Reason-code-bearing errors must use `JSONResponse(status_code=N, content={"reason_code":..., "detail":...})`, never `raise HTTPException(status_code=N, detail={"reason_code":...})`. The latter wraps the body under `body.detail.reason_code` and breaks the UI toast helper which reads `body.reason_code` top-level. Audit point: any new `HTTPException(detail=dict)` in `server.py` should be reviewed for whether the dict carries `reason_code`.

### 13.3 The quote_token saga (PR #34 → #35 → #47 → #48 → 654d72d2)

The token-signed quote surface has been re-shipped and reverted twice. Today:

- Backend mint route exists (`server.py:2539` `/orders/quote`) — fully functional, returns a signed token.
- Backend verify path exists in `_validate_non_combo_quote` (`:2475`) — fully wired.
- UI place path **does not** mint, attach, or verify tokens. `placeOrderContract.ts:35` would forward a token if present, but no caller sets one.
- `web/CLAUDE.md` line 120 explicitly warns against re-shipping as-is.

This means there's a partially-dead code path on both sides. It's a structural risk: low cost to delete, low cost to leave, but the longer it stays the more tempting it is to re-wire without re-doing the analysis that led to two reverts.

### 13.4 The activity poller and TWS divergence

`ib_activity_mirror` is the duct tape between FastAPI's local order ledger and what IB actually has. It works for fills (idempotent on `exec_id`) and for late-arriving CommissionReports. It does **not** mirror:

- TWS-side cancels of working orders (§10.4).
- TWS-side modifies on Xenon-authored UUID rows — `register_from_snapshot` deliberately skips updating UUID rows because those have `modify_sequence` invariants we don't want silently violated. Visible side-effect: the UI may show a stale price for an order the user changed in TWS.

Both are tracked in `src/xenon/api/CLAUDE.md` lines 36–44 as known gaps.

### 13.5 The asymmetric writers of `order_fills.metadata`

`record_external_fills`, `single_leg_rehydrate`, and `record_fill` each populate `order_fills.metadata` with different richness. Incident #16 (PR #71) added `_enrich_records_via_ib` to single-leg rehydrate so that snapshot-mirrored fills carry strike/right; the canonical consolidation is still open work. The risk: a future bug in any one writer means historical trades render with "Unknown Unknown" structure labels (the surface that triggered #16).

### 13.6 The hardcoded `user_id="local"`

`server.py:2822` (`_orders_place_from_body`):

```python
user_id = "local"
```

This is fine for single-user dev but blocks multi-tenant scenarios. The idempotency key is `(broker, account_env, broker_account, user_id, client_attempt_id)`, so different users can share a `client_attempt_id` only because they all collide on `user_id="local"`. Tracked as **C-6 backlog**.

### 13.7 Substring CI guards

`order_path_caller_allowlist.py` matches the _string_ `xenon-ib-place-order` and a few import patterns (regex). It will miss obfuscated invocations (e.g. building the binary name dynamically). **C-7 backlog** is to upgrade to AST call-site analysis. Risk is low — no one accidentally builds binary names dynamically — but it's a thin defense.

### 13.8 Modify race on subprocess failure

`_orders_modify_from_body` (`server.py:3367–3384`):

- `apply_modify` advances the DB sequence to the new value.
- Subprocess fails (e.g. IB connection drop).
- We **don't roll back** — to prevent a client retry at the old sequence from being treated as "fresh" and double-applying.
- Instead, the 503 response includes `applied_sequence: <new>` so the client counter syncs.

This is correct, but it leaves a window where the DB sequence is `N` and IB never saw the modify. The next user-initiated retry uses `N+1` and goes through cleanly. The risk is purely cosmetic: if the user reads the DB-side modify_sequence and assumes it reflects IB state, they'll be wrong for the duration of one failed cycle. Not yet observed in production but worth knowing.

### 13.9 The combo modify "CRITICAL" path

§10.2's cancel-then-place. The "CRITICAL: Original cancelled, replacement FAILED" message is the explicit data-loss alarm. There is no rollback. **C-2.2 backlog** is the proper fix.

### 13.10 Schema invariants assumed but not enforced everywhere

- `record_fill` (`orders_store.py:573`) raises `ValueError` when `account_env == "legacy_unknown"`. Other writers don't enforce this — they accept legacy_unknown and rely on CHECK constraints to catch it.
- `account_snapshots` does not have an FK to `nav_history`; the bankroll resolver assumes the latest snapshot row is the truth. If the IB sync is misconfigured for a scope, the resolver returns NULL → falls back to $10k.

### 13.11 No continuous PENDING reaper (verified 2026-05-02)

The `PENDING_TIMEOUT_SECONDS = 60` constant lives in `single_leg_rehydrate.py:31`. It is consulted **only** during `rehydrate_on_boot`, which the FastAPI lifespan calls once at startup (and via the dev probe `POST /dev/rehydrate/synthetic`). The activity poller does **not** sweep stale `PENDING` rows.

**Implication.** If a place subprocess hangs (network drop mid-`placeOrder`, or IB Gateway becomes unresponsive after `client.connect()` succeeded), the row sits `PENDING` until the next FastAPI restart. The `client_attempt_id` is held — subsequent retries with the same UUID hit `duplicate` (the row is not in `_TERMINAL_STATES`) and silently no-op. The user sees nothing in IB, and nothing in the UI explains why their resubmit didn't take effect.

The mechanical fix is a 30 s background task that runs `UPDATE order_submissions SET state='FAILED', reason_code='PENDING_TIMEOUT' WHERE state='PENDING' AND updated_at < now() - interval '60s'`. Pure Postgres, no IB dependency, idempotent. The reason code already exists in the rehydrate path so toast copy is downstream-ready.

### 13.12 Subprocess concurrency is unbounded

`_run_ib_script_with_recovery` (`server.py:4034`) is called directly from `_orders_place_from_body` (`:2886`), `_orders_cancel_from_body` (`:3038`), and `_orders_modify_from_body` (`:3367`) with no surrounding semaphore. Compare: UW on-demand OI calls use `asyncio.Semaphore(3)` (`server.py:810`).

The clientId range for these subprocesses is **20–49 = 30 IDs** (see §8.1). 30 concurrent place attempts will exhaust the range; ib_async `connect()` will then either raise or block on the next allocation attempt with no fairness or queueing. There is no `IB_BUSY` 503 — the failure mode is connect timeouts piling up under load.

The mechanical fix is `asyncio.Semaphore(N)` around the place/modify/cancel script calls (N = 20 leaves headroom for cancel and modify in the same range). Add a counter metric so we can size N empirically.

### 13.13 `submit_combo` is a parallel order-creation surface

The combo-wizard route `/orders/wizard/{session_id}/submit` (`src/xenon/api/routes/wizard.py:91`) calls `xenon.execution.combo_wizard.session.submit_combo` (`combo_wizard/session.py:293`) — a second order-creating entry point that is **not** `_orders_place_from_body`.

Because `submit_combo` is reached via FastAPI HTTP, route-level `Depends` (auth, scope) does fire. But the 8-stage validation pipeline (regime gate → preflight → quote gate → reserve_attempt) is implemented in the place handler — `submit_combo` reproduces the gates inline. Any change to the gate sequence in `_orders_place_from_body` must be mirrored in `submit_combo`, or the wizard becomes a regression hazard. (Incident #12 — combo Gate-4 not reaching in-process callers — is the precedent.)

A future consolidation: extract the 8-stage pipeline as a pure async function and invoke from both routes. The function-level call pattern proven by #12 fix is the template.

### 13.14 `acknowledge_limit_override` audit lives outside `regime_overrides`

When the place gate hits `LIMIT_OUT_OF_BAND` and the body carries `acknowledge_limit_override: true`, the order proceeds and a `PREFLIGHT_ACK_LIMIT` row lands in `xenon.order_events` (`server.py:2864`). It does **not** write `xenon.regime_overrides`.

Architectural inconsistency: regime overrides land in `regime_overrides`, limit-band overrides land in `order_events`. An audit query "show me every override the user accepted on this account" must hit both tables, with different schemas and join keys. The cleaner schema would extend `regime_overrides` with a `kind` column (`regime | limit_band`) and unify the writer surface.

### 13.15 Activity-poller latency budget unanalyzed

The fill-to-UI latency budget is the sum of two polls:

- **Server-side poller** — `XENON_IB_ACTIVITY_POLL_S` defaults to 60 s. A fill that arrives 1 s after a poll waits ~59 s for the next tick before `record_external_fills` writes it.
- **Client-side `/orders` poll** — cadence not centralized in this pass; route-handler `POST /orders/refresh` after every mutation tightens the place/cancel/modify echo, but **passive** fills (arriving with no user mutation in flight) wait for the ambient UI poll.

Worst-case fill-to-toast latency is **~120 s**. For limit fills in fast-moving names, this is a long blind window. The doc's TL;DR notes "no live WS push for orders" as a fragility but does not quantify the cost — it is the single largest user-visible latency in the system.

The fix is a server-pushed `order` channel on the existing WebSocket surface (the price WS already exists at `web/lib/usePrices.ts:354`). Backend would emit on `mark_submitted`, `mark_terminal`, `record_fill`. Frontend would subscribe and discard polling. Compresses fill latency from ~120 s → sub-second.

### 13.16 Open follow-ups (backlog)

| Item                                        | Where                                                                                                                 | Source                            |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| C-2.2 cancel-then-place data loss           | `web/app/api/orders/modify/route.ts:142–184`                                                                          | `docs/todo-backlog.md` 2026-05-01 |
| C-2.3 modify override audit insert          | `apply_modify` does not write `regime_overrides`                                                                      | same                              |
| C-5 hedge structure registry                | `regime_gate.py:_HEDGE_PUT_UNDERLYINGS` is a frozenset; should be sourced from `docs/trading/options-structures.json` | same                              |
| C-6 hardcoded `user_id="local"`             | `server.py:2822`                                                                                                      | same                              |
| C-7 substring CI guard → AST                | `scripts/checks/order_path_caller_allowlist.py`                                                                       | same                              |
| TWS-cancel mirroring                        | `ib_activity_mirror.py`                                                                                               | `src/xenon/api/CLAUDE.md:44`      |
| `order_fills.metadata` writer consolidation | `record_fill / record_external_fills / single_leg_rehydrate`                                                          | incident-history.md row #16       |
| Naked-short BUY 1 QQQ live confirmation     | `web/lib/nakedShortGuard.ts` + UI form                                                                                | session memory                    |

---

## 14. Known Issues + Next Steps

Punch list, ranked by risk × impact (this version reflects a 2026-05-02 verification pass — items 1–4 were under-emphasized in the original draft; see §13.11–§13.15):

**Tier 1 — reliability gaps with user-visible failure modes:**

1. **No continuous PENDING reaper (§13.11)** — subprocess hangs leave rows stuck `PENDING` until the next FastAPI restart. Same `client_attempt_id` retries silently no-op. Mechanical fix: 30 s background `UPDATE ... SET state='FAILED' WHERE state='PENDING' AND updated_at < now() - 60s`. Pure Postgres.
2. **Subprocess concurrency unbounded (§13.12)** — order place/modify/cancel calls have no semaphore; clientId range 20–49 (30 IDs) is exhaustible under load. Fix: `asyncio.Semaphore(20)` + `IB_BUSY` 503 + counter metric.
3. **TWS-cancel not mirrored** — `WORKING` rows for orders cancelled outside Xenon become phantoms until restart. Disambiguate with `order_fills` for the same `(perm_id, scope)` + idle-grace window.
4. **No order-event WS push (§13.15)** — fill-to-toast latency budget is up to ~120 s (60 s server poll + 60 s UI poll). Extend the existing price WS surface with an `order` channel; emit on `mark_submitted/mark_terminal/record_fill`.

**Tier 2 — audit / consistency / parallel-surface parity:**

5. **C-2.2 combo modify cancel-then-place** — place-failure after successful cancel leaves the user without their order. Persist legs in `order_submissions.metadata` (or a `combo_legs` sibling) and auto-replay on failure.
6. **C-2.3 modify override audit** — `_run_modify_regime_gate` override path does not write `regime_overrides`. `override_supported: false` is the current honest signal; the structural fix is plumbing override fields through `apply_modify` into the audit table.
7. **`acknowledge_limit_override` audit fragmentation (§13.14)** — limit-band overrides land in `order_events`, not `regime_overrides`. Extend `regime_overrides` with `kind ∈ {regime, limit_band}` and unify the writer surface.
8. **`submit_combo` gate parity (§13.13)** — wizard surface is a second order-creating route. Audit it line-by-line against the 8-stage pipeline and extract a shared async function.
9. **`order_fills.metadata` writers asymmetric** — incident #16's open follow-up. Three writers, different richness, root cause of the "Stock" structure-label fall-through.

**Tier 3 — long-tail follow-ups (already on backlog):**

10. **C-5 hedge registry** — `_HEDGE_PUT_UNDERLYINGS` should pull from `docs/trading/options-structures.json`.
11. **C-6 `user_id="local"`** — blocks multi-tenant. Idempotency-key collision risk if anything ever goes multi-user.
12. **C-7 substring CI guard** — fragile to obfuscation. Upgrade to AST call-site analysis.
13. **Modify race on subprocess failure** — DB sequence advances, IB doesn't. Cosmetic but worth flagging.
14. **`quote_token` partially-dead code** — both sides wired, no UI caller. Either delete or re-ship after re-doing the #34/#47 analysis.
15. **`reqContractDetailsAsync` not plumbed** — tick stub returns 0.01 universally. Acceptable until edge instruments dominate the trade mix.

Cross-referenced into `docs/reference/order-path-incident-history.md` for chronology, and `docs/todo-backlog.md` (inbox 2026-05-01) for active queue items.
