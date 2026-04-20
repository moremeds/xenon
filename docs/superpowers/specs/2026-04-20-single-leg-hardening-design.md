# Single-Leg Order Hardening — Design

Date: 2026-04-20
Status: Draft v0.2 (post-tribunal), awaiting review
Precedes: `docs/superpowers/specs/2026-04-20-leg-wizard-design.md`
Program: part of the Order Execution Foundation program. Sub-plans
tracked in
`docs/superpowers/plans/2026-04-20-order-execution-foundation-master.md`.
F0–F7 of that master plan implement this spec.
Related code: `web/components/ticker-detail/OrderTab.tsx`,
`web/lib/order/*`, `web/lib/nakedShortGuard.ts`,
`web/app/api/orders/place/route.ts`,
`web/app/api/orders/cancel/route.ts`,
`src/xenon/api/server.py`,
`src/xenon/execution/ib_place_order.py`,
`src/xenon/execution/ib_order_manage.py`,
`src/xenon/execution/naked_short_audit.py`

**Changelog v0.1 → v0.2** — applied 16 tribunal findings (Codex + Claude):
atomic idempotency reservation (pre-submit, not post), id rotation on
terminal response + field change, STK-for-index reject, existing-short-call
accounting, working-order reservations, server-fetched quotes, remove
clientId rotation in cancel/modify, UNKNOWN reconcile state, expanded
orders_submissions schema, NOT NULL + local principal, tick-grid limit
band, audit parity regression, modify-path semantics,
orders.json→duckdb migration, USO K-1 note.

## 1. Purpose & non-goals

**Purpose.** Close real gaps in today's single-leg stock / single-leg
option path so the upcoming Leg Wizard (multi-leg) can assume a clean
foundation: authoritative server-side gates, atomic idempotency, honest
error propagation, server-truth quote freshness, cancel/modify ownership
respect, and restart-safe reconciliation. Each gap bleeds on every order
today; the wizard amplifies them.

**Non-goals.**

- Not a multi-leg feature. The wizard spec owns combos, sequencing,
  protection brackets, residual BAG.
- Not a UI redesign of `OrderTab`. Minimum widgets to support new gates.
- Not a rewrite of the cancel/modify subprocess architecture. The
  original-clientId ownership rule (`src/xenon/api/CLAUDE.md`) is
  preserved — **this spec fixes failure propagation AND removes
  incorrect rotation logic; it never rotates away from the owner.**
- Not an order router. SMART stays the only route.

## 2. Trading universe assumptions

V1 universe, user-declared:

| Ticker | Type           | Option style           | Stock leg? | Note      |
| ------ | -------------- | ---------------------- | ---------- | --------- |
| SPX    | Index          | European, cash-settled | **No**     | Index opt |
| NDX    | Index          | European, cash-settled | **No**     | Index opt |
| RUT    | Index          | European, cash-settled | **No**     | Index opt |
| SPY    | ETF            | American, deliverable  | Yes        |           |
| QQQ    | ETF            | American, deliverable  | Yes        |           |
| IWM    | ETF            | American, deliverable  | Yes        |           |
| GLD    | ETF            | American, deliverable  | Yes        |           |
| USO    | ETF (K-1 fund) | American, deliverable  | Yes        | K-1 tax\* |
| SIL    | ETF            | American, deliverable  | Yes        |           |

\* USO is a commodity pool partnership (Schedule K-1). Option exercise
delivers USO shares normally, but resulting stock positions carry K-1
tax reporting. Noted in `universe.py`; no execution-path special case.

Design implications:

1. **Index options have no stock leg.** A short SPX call can **only** be
   covered by a long SPX call. Stock coverage is physically impossible
   (SPX shares don't exist). Gate 4 must branch on `is_index`.
2. **`security_type=STK` with `ticker in INDEX_UNIVERSE` must be
   rejected at the API boundary before any other logic.** The frontend
   can emit `type:"stock"` for any ticker in `buildSingleLegOrderPayload`;
   server cannot trust this.
3. **All nine tickers are top-decile liquid.** Quote-freshness default
   500ms RTH is realistic.
4. **No earnings, small dividends.** Out of scope for this spec.

Single source of truth: `src/xenon/execution/universe.py`:

```python
UNIVERSE = {
    "SPX": {"type": "INDEX", "is_index": True,  "cash_settled": True,  "multiplier": 100, "k1": False},
    "NDX": {"type": "INDEX", "is_index": True,  "cash_settled": True,  "multiplier": 100, "k1": False},
    "RUT": {"type": "INDEX", "is_index": True,  "cash_settled": True,  "multiplier": 100, "k1": False},
    "SPY": {"type": "ETF",   "is_index": False, "cash_settled": False, "multiplier": 100, "k1": False},
    "QQQ": {"type": "ETF",   "is_index": False, "cash_settled": False, "multiplier": 100, "k1": False},
    "IWM": {"type": "ETF",   "is_index": False, "cash_settled": False, "multiplier": 100, "k1": False},
    "GLD": {"type": "ETF",   "is_index": False, "cash_settled": False, "multiplier": 100, "k1": False},
    "USO": {"type": "ETF",   "is_index": False, "cash_settled": False, "multiplier": 100, "k1": True},
    "SIL": {"type": "ETF",   "is_index": False, "cash_settled": False, "multiplier": 100, "k1": False},
}
INDEX_UNIVERSE = {t for t, meta in UNIVERSE.items() if meta["is_index"]}
```

Frontend mirrors via `web/lib/universe.ts`, generated from the Python
registry by a build step to prevent drift.

## 3. Gap inventory and resolution map

| #   | Gap                                                          | Owner module                                       | Section |
| --- | ------------------------------------------------------------ | -------------------------------------------------- | ------- |
| 1   | Gate 4 UI-only; not re-evaluated server-side                 | `execution/preflight.py` (new)                     | §4, §5  |
| 2   | No idempotency key → double-click = two IB orders            | `execution/preflight.py` + route                   | §6      |
| 3   | Manual limit not sanity-checked vs live bid/ask              | `execution/quote_guard.py` (new, shared w/ wizard) | §7      |
| 4   | Cancel/modify subprocess connectivity failure swallowed      | `execution/ib_order_manage.py` + route             | §8      |
| 5   | Stock SELL quantity not pre-checked vs held shares           | `execution/preflight.py`                           | §5      |
| 6   | Expiry normalized in 4 places independently                  | `execution/contract_normalize.py` (new)            | §9      |
| 7   | Upstream IB error detail collapsed to generic 500 in Next.js | `web/app/api/orders/place/route.ts`                | §10     |
| 8   | No quote-freshness gate for single-leg submit                | `execution/quote_guard.py`                         | §7      |
| 9   | No single-leg reconciliation on FastAPI restart              | `execution/single_leg_rehydrate.py` (new)          | §11     |
| 10  | Cancel/modify failure has no user-visible state              | route + toast                                      | §8      |

Shared axis across all ten: **authoritative server-side pre-submission
gate with atomic state.**

## 4. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Frontend (OrderTab + NewOrderForm)                          │
│  • client_attempt_id (uuid) in useRef                        │
│    rotate: form-open | ticker-change | qty/price edit after  │
│            submit | terminal response (FILLED/REJECTED/      │
│            CANCELLED)                                        │
│  • submit disabled while request in flight                   │
│  • quote_token: signed snapshot issued by server (§7)        │
└─────│────────────────────────────────────────────────────────┘
      │  POST /api/orders/place { …, client_attempt_id, quote_token }
┌─────▼────────────────────────────────────────────────────────┐
│  Next.js route — forwards client_attempt_id + quote_token    │
│  preserves upstream status + detail on error                 │
└─────│────────────────────────────────────────────────────────┘
      │
┌─────▼────────────────────────────────────────────────────────┐
│  FastAPI /orders/place                                       │
│  ├─ PRE-SUBMIT: preflight.evaluate(req)                      │
│  │     ① Universe check (ticker in registry; STK-for-index   │
│  │        rejected here)                                     │
│  │     ② Gate 4 with existing-short + working-order          │
│  │        reservations (reads orders_submissions WORKING)    │
│  │     ③ quote_guard: validate server-signed quote_token     │
│  │        (freshness, crossed, limit-band incl. tick grid)   │
│  │     ④ idempotency: INSERT ... ON CONFLICT DO NOTHING      │
│  │        → winner proceeds; loser reads existing row and    │
│  │          returns {duplicate_of, state}                    │
│  ├─ if winner: subprocess xenon-ib-place-order               │
│  ├─ on subprocess result: UPDATE row with ib_order_id/state  │
│  └─ if subprocess connectivity failure → row state=FAILED    │
│                                                              │
│  Quote token issuance:                                       │
│  GET /orders/quote?ticker&contract — server fetches from     │
│  IB pool, signs {bid,ask,ts_server,bid_size,ask_size,sig},   │
│  TTL matches freshness default                               │
│                                                              │
│  /orders/cancel and /orders/modify:                          │
│  ├─ discover original clientId from trade.order.clientId     │
│  ├─ subprocess reconnects AS the original clientId           │
│  │   (never rotates — ownership rule, src/xenon/api/CLAUDE.md)│
│  ├─ on clientId-in-use (326): back off + retry same owner    │
│  ├─ classify exit: connection | ib_reject | ownership        │
│  │   → HTTP 503 | 4xx | 409 accordingly                      │
│  └─ confirm via refreshed open-order snapshot (CLAUDE.md §3) │
└─────│────────────────────────────────────────────────────────┘
      │
      └──► IB Gateway
```

### 4.1 New modules

```
src/xenon/execution/
  preflight.py                — synchronous evaluate() → Verdict
  quote_guard.py              — SHARED with leg-wizard; single-leg first
  quote_tokens.py             — server-signed snapshot issuance + verify
  contract_normalize.py       — expiry / symbol / multiplier canonical
  single_leg_rehydrate.py     — FastAPI-boot reconciler
  universe.py                 — the 9-ticker registry (§2)
```

## 5. Pre-submission gate (preflight.py)

```python
class PreflightRequest(BaseModel):
    client_attempt_id: str
    ticker: str
    security_type: Literal["STK", "OPT"]
    action: Literal["BUY", "SELL"]
    quantity: int
    right: Literal["CALL", "PUT"] | None
    expiry: str | None                  # normalized YYYYMMDD
    strike: Decimal | None
    multiplier: int                     # from universe.py
    con_id: int | None                  # IB contract id when known
    limit_price: Decimal
    quote_token: str                    # server-signed, see §7

class Verdict(BaseModel):
    accept: bool
    reason_code: str | None             # enum; see §6
    reason_detail: str | None
    duplicate_of: str | None            # prior ib_order_id if idempotent hit
    reserved_submission_id: str | None  # set when preflight inserts row
```

### 5.1 Universe and Gate 4 — fixed pseudocode

```
# ① Universe gate
if ticker not in UNIVERSE:                      BLOCK "UNIVERSE_UNKNOWN"
if security_type == "STK" and UNIVERSE[ticker]["is_index"]:
                                                BLOCK "INDEX_HAS_NO_STOCK"

# ② BUY is always allowed (no short exposure created)
if action == "BUY":                             return ACCEPT

# ③ Gate 4 — working-order-aware cover math
portfolio = ib_pool.live_portfolio()            # not portfolio.json
reservations = orders_submissions.working_reservations(user, ticker)

if security_type == "STK":
    held   = portfolio.long_shares(ticker)
    working_sell_shares = reservations.stock_sell_qty
    available = held - working_sell_shares
    if quantity > available:                    BLOCK "INSUFFICIENT_SHARES"
    return ACCEPT

# security_type == "OPT", action == "SELL"
if right == "PUT":
    required_cash = strike * multiplier * quantity
    working_put_cash = reservations.short_put_cash_required
    if available_funds - working_put_cash < required_cash:
                                                BLOCK "INSUFFICIENT_CASH"
    return ACCEPT

# right == "CALL" → must be covered
existing_short_calls = portfolio.short_calls(ticker, expiry_any=True)
working_short_calls  = reservations.short_call_qty
long_calls_same_exp  = portfolio.long_calls(ticker, expiry=expiry)
working_long_call_closes = reservations.long_call_close_qty_same_exp

# Long calls consumed as cover already:
long_call_cover_available = long_calls_same_exp - working_long_call_closes

if UNIVERSE[ticker]["is_index"]:
    # Stock coverage impossible for index options
    total_short_after  = existing_short_calls + working_short_calls + quantity
    if long_call_cover_available < total_short_after:
                                                BLOCK "INDEX_CALL_UNCOVERED"
    return ACCEPT

# ETF: stock OR long calls cover
shares = portfolio.long_shares(ticker)
working_sell_shares = reservations.stock_sell_qty
share_cover_units = (shares - working_sell_shares) // multiplier
total_cover = share_cover_units + long_call_cover_available
total_short_after = existing_short_calls + working_short_calls + quantity
if total_cover < total_short_after:             BLOCK "ETF_CALL_UNCOVERED"
return ACCEPT
```

This eliminates three bugs from v0.1:

- Index STK now rejected at boundary (§2 implication surfaced).
- Existing short calls subtract from cover (parity with
  `nakedShortGuard.ts:countExistingShortCalls`).
- Working-order reservations read from `orders_submissions` so two
  resting `SELL 100 SPY` cannot both pass against 100 shares.

### 5.2 Quantity-vs-held — folded into §5.1

The shares / cash paths above use **live portfolio from IB pool**, not
`portfolio.json`. Reservations from duckdb complete the picture.

### 5.3 Idempotency — atomic reservation before submit

**Problem v0.1 had:** concurrent POSTs could both miss the lookup and
both submit to IB.

**Fix — pre-submit reservation:**

```sql
INSERT INTO orders_submissions (
    submission_id, user_id, client_attempt_id,
    ticker, security_type, action, quantity,
    expiry, strike, right, multiplier, con_id,
    limit_price, state, submitted_at, updated_at
) VALUES (
    :sid, :uid, :cid, …, 'PENDING', now(), now()
)
ON CONFLICT (user_id, client_attempt_id) DO NOTHING
RETURNING submission_id;
```

- Winner gets a `submission_id`; proceeds to subprocess call.
- Loser reads the existing row. Return:
  - If existing row state ∈ `{PENDING, WORKING, PARTIALLY_FILLED, FILLED}`:
    `200 { duplicate_of: ib_order_id | null, state }`.
  - If existing row state ∈ `{REJECTED, CANCELLED, FAILED}`:
    `409 { reason_code: "ATTEMPT_ID_TERMINAL" }` — client must rotate
    before retrying.

**`client_attempt_id` lifecycle (v0.2 — tightened):**

Rotate the id in the frontend on:

- Form open / reset / ticker change (as v0.1).
- **Any qty, limit_price, right, strike, or expiry edit AFTER a submit
  has been issued** (new — prevents silent collapse of intentional
  re-submits after fill/reject).
- **Receipt of any terminal response** (FILLED, REJECTED, CANCELLED,
  FAILED) — new; ensures next submit is a fresh attempt by default.

Double-click protection still works: the in-flight window keeps the
same id, so two POSTs from the same render frame collide on the unique
key.

### 5.4 Modify path — distinct semantics

Modify reuses the original IB `order_id` and is NOT deduplicated by
`client_attempt_id`. It has its own contention model:

- `POST /api/orders/modify { order_id, new_limit_price?, new_quantity?,
modify_sequence }`.
- `modify_sequence` is a monotonically increasing integer per
  `order_id`, generated client-side and echoed server-side.
- Server rejects modify with a `modify_sequence ≤` the last-applied
  value for that `order_id` → `409 { reason_code: "MODIFY_STALE" }`.
- Modify still passes through preflight Gate 4 (new qty may overbook
  cover) and quote_guard (new price band).

Cancel similarly has no `client_attempt_id` — it's keyed on `order_id`
with a simple "already-cancel-pending" in-flight check.

## 6. Frontend changes (minimum)

- `NewOrderForm` holds `client_attempt_id` in a `useRef`.
  - Init: `crypto.randomUUID()` on mount.
  - Rotate triggers: ticker change; reset; qty/price/right/strike/expiry
    edit after any submit; terminal response from server.
- Submit button disabled while request in flight.
- New reason codes (server-enumerated, UI maps to copy):

| code                 | severity | toast text                                                |
| -------------------- | -------- | --------------------------------------------------------- |
| UNIVERSE_UNKNOWN     | error    | Ticker not in V1 universe.                                |
| INDEX_HAS_NO_STOCK   | error    | Index options can't trade as stock (SPX/NDX/RUT).         |
| INSUFFICIENT_SHARES  | error    | SELL N shares exceeds held (+ working orders).            |
| INSUFFICIENT_CASH    | error    | Cash-secured put exceeds available funds.                 |
| INDEX_CALL_UNCOVERED | error    | Short index call requires long-call cover (same expiry).  |
| ETF_CALL_UNCOVERED   | error    | Short call uncovered after accounting for working orders. |
| STALE_QUOTE          | error    | Quote expired; refreshing.                                |
| LIMIT_OUT_OF_BAND    | warn     | Limit too far from market. Acknowledge to override.       |
| LIMIT_OFF_TICK       | error    | Price not on contract tick grid.                          |
| ATTEMPT_ID_TERMINAL  | info     | Previous attempt ended. Rotating id.                      |
| MODIFY_STALE         | error    | Modify sequence stale; refresh and retry.                 |
| IB_CONNECTION        | error    | IB connection lost — retry.                               |
| OWNERSHIP            | error    | Order owned by another session.                           |

- Cancel/modify buttons render explicit FAILED state on 503/409
  (§8), not optimistic success.

## 7. Quote freshness (quote_guard.py + quote_tokens.py)

**Problem v0.1 had:** freshness was based on client-supplied
`QuoteSnapshot` — a stale or forged quote passes trivially.

**Fix — server-signed quote tokens:**

- Frontend calls `GET /orders/quote?ticker=…&con_id=…` to fetch a
  current snapshot.
- Server reads from IB pool, captures `ts_server` (monotonic),
  returns a signed token: `HMAC(secret, {bid, ask, ts_server,
bid_size, ask_size, con_id, ticker})`.
- Token TTL = freshness default (500ms RTH, 5000ms extended).
- Client sends the token on submit. `quote_guard.verify_token()` checks
  signature and server-side age. Client-side timestamps are never
  trusted for gating.

Freshness gates (server-verified token only):

- `ts_server` within `max_age_ms` (500ms RTH, 5000ms pre/post).
- `bid_size > 0`, `ask_size > 0`.
- Not crossed (`bid <= ask`).
- No NaN/null.

Limit-price band (applied after freshness):

- **Tick-grid normalization first.** Read contract `minTick` from IB
  (`reqContractDetails.marketRuleId` → rule table) via `ib_pool`.
  Cache per-con_id for 24h. Reject if the proposed price is not on
  the tick grid (`LIMIT_OFF_TICK`).
- **Percent cap AND absolute-tick cap (both must hold):**
  - BUY: `limit ≤ min(ask × 1.05, ask + 2 × minTick)`.
  - SELL: `limit ≥ max(bid × 0.95, bid - 2 × minTick)`.
- On cheap options ($0.05 contracts), 2×minTick dominates; avoids the
  "$0.052 rounds to $0.10" trap. On expensive contracts, 5% dominates.
- `LIMIT_OUT_OF_BAND` is overridable with an explicit acknowledgement
  checkbox; override is audit-logged (`orders_events` kind
  `PREFLIGHT_ACK_LIMIT`).

Market-hours:

- OPT: block outside 9:30–16:00 ET weekdays.
- STK: allow pre/post with relaxed freshness, clearly labelled in UI.

## 8. Cancel / modify failure propagation — owner-preserving

**Ownership invariant (unchanged, re-stated):** IB `cancelOrder` and
`placeOrder` (modify) are scoped by the clientId that placed the order.
The subprocess MUST reconnect as `trade.order.clientId` — there is no
valid "retry with a fresh clientId" path for cancel/modify. v0.1's
proposal to rotate clientId on 326 was wrong and is removed.

Flow:

1. Route discovers target order's `original_client_id` via `ib_pool`
   (master client=0 can SEE all orders via `reqAllOpenOrders`).
2. Optional telemetry probe: `socket.connect(127.0.0.1, ib_port, 200ms)`.
   If it fails, UI gets early "IB connection likely down" _warning_;
   subprocess still runs (probe is not authoritative — see ISSUE-12).
3. Subprocess `xenon-ib-order-manage --client-id <original>` attempts
   reconnection AS the owner.
4. If `326 clientId in use`: back off 500ms, retry up to 3× — still as
   the same owner. If all fail, return `409 OWNERSHIP` with detail
   "another session holds the owner clientId; try again shortly". This
   is semantically correct: the operation is blocked by ownership
   contention, not connectivity.
5. If any connectivity error (no socket, TWS down, gateway reject on
   handshake): return `503 IB_CONNECTION` with detail. UI shows FAILED
   state, button re-enables, no optimistic success.
6. If IB semantic reject (201, 10147 misuse, etc.): return `4xx
IB_REJECT` with upstream code + text preserved.
7. Confirm via refreshed open-order snapshot (CLAUDE.md §3):
   disappearance after a submitted cancel = success, even if original
   `Trade` object is stale.

**Dropped from v0.1:** the "gateway healthy → suppress error" cooldown
is removed from this path. That heuristic belongs to gateway restart
throttling, not to user-facing cancel results.

The ib_pool acquires an internal registry of "owner clientIds
currently busy" so concurrent cancel+audit on the same order don't
both race for clientId 25 (audit uses `CLIENT_IDS["ib_order_manage"]=25`
per `naked_short_audit.py:242`). Either serialize at the pool or
allocate audit to a distinct range 26–29.

## 9. Contract normalization (contract_normalize.py)

One callsite for:

- Expiry `YYYYMMDD` IB canonical, no dashes, no shortcuts.
- Symbol canonicalization; SPX weekly (`SPXW`) handled inside client.
- Multiplier lookup from `universe.py`.

Called at the API boundary. `OrderTab.tsx`, `ib_place_order.py`, and
`nakedShortGuard.ts` stop normalizing locally. A build-time check
fails CI if any of those three files reimplements a regex replace on
expiry.

## 10. Error propagation (route.ts)

Replace generic-500 catches in `orders/place`, `orders/cancel`,
`orders/modify` routes with:

- On `XenonApiError`: preserve upstream status (4xx / 5xx / 503) and
  `detail` into NextResponse verbatim.
- Other errors: 500 with `{error: "internal", request_id}`.
- Log full error chain with `request_id`.

## 11. Rehydration (single_leg_rehydrate.py)

On FastAPI boot, for every row in `orders_submissions` with state in
`{PENDING, WORKING, PARTIALLY_FILLED}`:

1. Fetch **three sources**: IB open orders, `executionDetails`, live
   positions.
2. Reconcile by `(perm_id, ib_order_id)`:
   - Found in open orders → copy live state.
   - Not in open orders, has executions matching perm_id → FILLED with
     exec qty + avg price.
   - Not in open orders, no executions, positions unchanged → marked
     `CANCELLED` (the cancel already happened).
   - **Not in open orders, no executions, BUT positions CHANGED for
     this ticker/contract** → state = `UNKNOWN`; emit
     `REHYDRATE_UNCERTAIN` event; UI shows banner "order state
     uncertain — reconcile manually". Never auto-terminalize as
     CANCELLED in this branch.
3. For rows in `PENDING` (no `ib_order_id` ever recorded) older than
   T=60s → state = `FAILED` with reason `PENDING_TIMEOUT`. These are
   rows where subprocess never returned an order id (likely
   connectivity failure during first submit). The `client_attempt_id`
   is retained; client may rotate and retry.
4. Log any source disagreement. All reconcile decisions emit
   `REHYDRATE_RECONCILED` rows in `orders_events`.

This reconciler is the template the wizard's `rehydrate.py` reuses —
helper `_reconcile_from_three_sources(perm_id, order_id)` lives here
and both consumers import it.

## 12. Data store

New DuckDB file: `data/orders.duckdb`:

```sql
CREATE TABLE orders_submissions (
  submission_id       TEXT PRIMARY KEY,       -- server uuid
  user_id             TEXT NOT NULL,           -- "local" sentinel when auth bypassed
  client_attempt_id   TEXT NOT NULL,
  ticker              TEXT NOT NULL,
  security_type       TEXT NOT NULL,           -- STK | OPT
  action              TEXT NOT NULL,           -- BUY | SELL
  quantity            INTEGER NOT NULL,
  expiry              TEXT,                    -- YYYYMMDD, option only
  strike              DECIMAL(18,4),           -- option only
  right               TEXT,                    -- CALL | PUT, option only
  multiplier          INTEGER NOT NULL,
  con_id              INTEGER,                 -- IB contract id when known
  placing_client_id   INTEGER,                 -- clientId subprocess used
  ib_order_id         TEXT,
  perm_id             TEXT,
  limit_price         DECIMAL(18,4) NOT NULL,
  state               TEXT NOT NULL,           -- PENDING|WORKING|FILLED|CANCELLED|REJECTED|PARTIALLY_FILLED|FAILED|UNKNOWN
  reason_code         TEXT,                    -- when terminal-failed
  filled_qty          INTEGER NOT NULL DEFAULT 0,
  avg_fill_price      DECIMAL(18,4),
  submitted_at        TIMESTAMP NOT NULL,
  updated_at          TIMESTAMP NOT NULL,
  UNIQUE (user_id, client_attempt_id)
);

CREATE INDEX ix_submissions_state_ticker ON orders_submissions(state, ticker);
CREATE INDEX ix_submissions_perm_id ON orders_submissions(perm_id);
CREATE INDEX ix_submissions_ib_order_id ON orders_submissions(ib_order_id);

CREATE TABLE orders_events (
  event_id       TEXT PRIMARY KEY,
  submission_id  TEXT NOT NULL REFERENCES orders_submissions(submission_id),
  kind           TEXT NOT NULL,               -- PREFLIGHT_BLOCKED|PREFLIGHT_ACK_LIMIT|SUBMITTED|FILL|REJECT|CANCEL|MODIFY|REHYDRATE_RECONCILED|REHYDRATE_UNCERTAIN
  detail         JSON,
  at             TIMESTAMP NOT NULL
);
CREATE INDEX ix_events_submission ON orders_events(submission_id, at);
```

Append-only semantics on `orders_events`. `orders_submissions` is
updated in place (state transitions) but never deleted.

**Local-principal for auth-bypass:** FastAPI localhost bypass
(`src/xenon/api/server.py`) sets `user_id="local"` explicitly when
Clerk is absent. This keeps `UNIQUE (user_id, client_attempt_id)`
deterministic and prevents null-key bypass.

**Schema includes full contract identity** (`expiry`, `strike`,
`right`, `multiplier`, `con_id`, `placing_client_id`) so rehydrate can
disambiguate same-ticker same-action same-price SPX option
submissions on the same day.

### 12.1 Migration from orders.json

`data/orders.json` (read today by `naked_short_audit.py:215-219`) stays
as the **authoritative file-based journal for the audit script** in
phase S0–S4. In S5 the audit is refactored to:

- Read from `orders.duckdb` for open-order enumeration (current
  working orders).
- Keep `orders.json` appended for backward compatibility until an
  explicit deprecation PR removes the reader.

This two-stage migration avoids double-write complexity and lets the
audit keep running unchanged through most of the rollout.

## 13. Testing

### 13.1 Mandatory regressions

Gate 4 and idempotency:

- 9×2×3×2 matrix: (ticker, action, {STK, OPT-CALL, OPT-PUT}, {covered,
  uncovered}).
- Index STK: `SPX` with `type=STK, action=BUY/SELL` → BLOCK
  `INDEX_HAS_NO_STOCK`, regardless of "position".
- SPX short call with SPX long call same expiry, different strike →
  ACCEPT.
- SPX short call with no long cover + no stock → BLOCK.
- SPY short call, 100 shares held, 1 existing short call → next short
  call BLOCKED (cover exhausted).
- Two concurrent `SELL 100 SPY` with same portfolio → first ACCEPT,
  second BLOCKED by working-order reservation.
- Double-click idempotency: two POSTs same `client_attempt_id` → ONE IB
  submission; loser returns `duplicate_of`.
- Terminal-reject then same `client_attempt_id` resubmit → 409
  `ATTEMPT_ID_TERMINAL`.
- Edit qty after submit → client rotates id; new submit is fresh.

Quote guard:

- Expired server token (> 500ms RTH age) → BLOCK `STALE_QUOTE`.
- Forged client timestamp w/ valid structure but no signature → BLOCK
  `STALE_QUOTE`.
- $0.05 option, limit = $0.052 → BLOCK `LIMIT_OFF_TICK`.
- $0.05 option, limit = $0.10 → ACCEPT (on-grid, 2-tick band allows).
- $10 option, limit = $12 → BLOCK `LIMIT_OUT_OF_BAND`; user ACK
  overrides; override row written to `orders_events`.

Cancel / modify:

- Gateway unreachable (socket probe fails, subprocess confirms) →
  503 `IB_CONNECTION`; UI FAILED; no optimistic success.
- Original clientId busy (326) → retry 3× as SAME owner → 409
  `OWNERSHIP` (never rotates clientId).
- IB semantic reject (201) → 4xx with upstream detail preserved.
- Cancel success via refreshed-open-orders disappearance even when
  original `Trade` object is stale.
- Modify with stale `modify_sequence` → 409 `MODIFY_STALE`.

Rehydrate:

- WORKING order, positions unchanged, no executions, not in open orders
  → CANCELLED.
- WORKING order, positions changed, no executions → UNKNOWN + banner
  (never auto-CANCELLED).
- PENDING row older than 60s with no ib_order_id → FAILED
  `PENDING_TIMEOUT`.
- FILLED via executions → state + fill_qty + avg_fill_price set.

Audit parity (new, required):

- `naked_short_audit.py` recognises long-call cover at same expiry, any
  strike (parity with `nakedShortGuard.ts:254-260`); regression for
  "preflight ACCEPTs, audit doesn't cancel".
- Audit runs on clientId 25 concurrent with cancel on some other order:
  neither blocks the other (pool registry coordination).

Error propagation:

- FastAPI returns 502 with detail → Next.js route preserves status and
  detail verbatim (no collapse to 500).

### 13.2 Test surfaces

- Python unit: `preflight`, `quote_guard`, `quote_tokens`,
  `contract_normalize`, `single_leg_rehydrate`, `ib_order_manage`
  failure classification.
- Python integration: `scripts/tests/test_single_leg_flow/` with
  `FakeIBClient` simulating connectivity drops, reject codes, partial
  fills, rehydrate scenarios.
- Frontend unit (Vitest): idempotency ref lifecycle, reason-code toast
  mapping, TS Gate 4 parity with Python fixtures (shared fixture file).
- E2E (Playwright): double-click guarded; FAILED cancel state; block
  toasts per reason code; tick-grid rejection UX.
- Coverage target 95% per Xenon policy.

## 14. Ship plan (phased)

| Phase  | Deliverable                                                                       | Blocks |
| ------ | --------------------------------------------------------------------------------- | ------ |
| **S0** | `universe.py` (+ generated `web/lib/universe.ts`) + `contract_normalize.py`       | none   |
| **S1** | `preflight.py` with universe/Gate 4/working-reservation logic. No IB side-effects | S0     |
| **S2** | `quote_tokens.py` + `quote_guard.py` + tick-grid lookup. Shared w/ wizard         | S1     |
| **S3** | Atomic idempotency (duckdb schema + pre-submit reservation + route wiring)        | S1     |
| **S4** | Cancel/modify owner-preserving failure propagation + UI FAILED state              | S0     |
| **S5** | `single_leg_rehydrate.py` + `orders_events` + audit parity fix (long-call cover)  | S3     |
| **S6** | Next.js error-detail preservation + reason-code toast library                     | S1, S4 |

S0–S4 unblock **leg-wizard P0**. S5–S6 can land in parallel with wizard P1.

## 15. Open questions / deferred

- **`orders.duckdb` dual-source with `orders.json`**: resolved (§12.1)
  — audit keeps reading json through S4; switches in S5.
- **Multi-user concurrency**: `user_id` is `NOT NULL`, "local" sentinel
  in bypass path. Real user ids (Clerk) scope uniqueness per account.
- **Universe expansion**: add to `universe.py`, regenerate TS mirror,
  Gate 4 tests regenerate from registry.
- **SPX AM vs PM settlement**: same Gate 4 rule (long-call cover).
  Settlement style does not change execution semantics.
- **IB clientId contention across audit + cancel**: noted in §8;
  implementation detail is to serialize at ib_pool or split ranges.
  Open question whether to change `CLIENT_IDS["ib_order_manage"]` to
  a less-trafficked slot.
- **Quote token secret rotation**: HMAC secret lives in `.env`;
  rotation breaks in-flight tokens. Accept short outage window
  during rotation; no JWK-style grace period in V1.
