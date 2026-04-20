# Leg-by-Leg Order Wizard — Design

Date: 2026-04-20
Status: Draft v0.2 (post-tribunal), awaiting review
Related code: `src/xenon/execution/`, `src/xenon/monitor_daemon/handlers/`,
`web/lib/order/`, `web/components/ticker-detail/OrderTab.tsx`,
`src/xenon/api/routes/`, `docs/trading/options-structures.json`,
`src/xenon/execution/naked_short_audit.py`, `web/lib/nakedShortGuard.ts`

Changelog v0.1 → v0.2: applied 15 fixes from Codex + Claude tribunal review
(partial fills, naked-short audit extension, atomic protection state,
SL semantics rename, TP math, jade-lizard whatIf, calendar/diagonal risk
reclass, residual BAG, BWB sequencing, quote freshness, rehydrate
reconciler, idempotency keys, test matrix extension, server-side Gate 4,
new `wizard_stop_monitor.py` handler).

## 1. Purpose & non-goals

**Purpose.** Place multi-leg option structures as **sequenced individual
legs** rather than a single IB BAG combo order, capturing better fills than
the synthetic combo market provides — while holding the existing Gate 1–4
guarantees across leg-risk windows, partial fills, and failure recovery.

**Non-goals.**

- Not a replacement for single-leg or stock order entry.
- Not a scanner or idea generator.
- Not a naked-short enabler. Gate 4 remains authoritative and is enforced
  **server-side** by the wizard planner, not just by the UI guard.
- Not a smart-order-router (SMART still routes each individual leg).
- **Does not guarantee loss containment** — see §8 "Risk Alert" semantics.

## 2. Scope

| In V1                                                                                              | Out of V1                                                       |
| -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Opens + closes                                                                                     | Rolls (combined close+open)                                     |
| Structures: vertical, long/iron butterfly, BWB, iron condor, calendar, diagonal, **jade lizard**   | Stock+option combos (collars, covered-call overwrite) — phase 2 |
| Mode A (decision-support; user clicks each leg)                                                    | Mode B (semi-auto anchor placement) — graduation                |
| Per-structure TP policy (fixed-risk spreads only); SL = "Risk Alert" assisted exit                 | Auto-chase, auto-convert-to-BAG, true auto-SL — graduation      |
| Dry-run / paper mode via `XENON_API_TEST_MODE`                                                     | Back-testing the legging algorithm                              |
| Sequencing: γ rule (SAFETY default; LIQUIDITY opt-in only for structures with **zero short legs**) | Algorithmic model-based TP for calendars/diagonals              |

## 3. Structure risk classification

"Contains short legs" and "risk class" are independent axes.

| Risk       | Structures                                                                                                     | Has short leg?  | Gate Notes                                                                                                                       |
| ---------- | -------------------------------------------------------------------------------------------------------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **Low**    | long vertical debit, long (debit) butterfly                                                                    | No              | Max loss = net debit                                                                                                             |
| **Medium** | short vertical, iron condor, iron butterfly, broken-wing butterfly (BWB), **long calendar**, **long diagonal** | Yes             | Defined risk; short legs hedged within structure or by opposite-expiry long. Calendars/diagonals have a short near-dated leg.    |
| **High**   | jade lizard                                                                                                    | Yes (short put) | Short put bounded only by cash collateral; must pass IB `whatIf` margin check (see §11). Requires explicit user acknowledgement. |

**LIQUIDITY sequencing opt-in is gated on "has short leg = No"** — that
excludes calendars, diagonals, BWB, and every Medium/High structure.

Risk-level badge is rendered in the plan-review step. **High** gates the
first-leg action behind a checkbox: _"I accept the cash-secured downside on
this structure."_ Acknowledgement is appended to the session audit log as a
`SessionEvent`.

## 4. High-level architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Frontend (web/)                                             │
│  WizardModal ── useWizardSession() ── SSE /wizard/stream    │
│     ▲                                                        │
│     │   /api/wizard/*  (Next.js route → xenonFetch)         │
└─────│────────────────────────────────────────────────────────┘
      │
┌─────▼────────────────────────────────────────────────────────┐
│  FastAPI (src/xenon/api/routes/wizard.py)                    │
│  plan / sessions / place-next / resolve-stall / abort        │
│  protect / stream / reconcile                                │
└─────│────────────────────────────────────────────────────────┘
      │
┌─────▼────────────────────────────────────────────────────────┐
│  Core engine (src/xenon/execution/leg_wizard/)               │
│  ├ planner.py        — structure → ordered leg plan (γ rule) │
│  │                    SERVER-SIDE Gate 4 + Gate 1 enforcement│
│  ├ session.py        — session state machine + audit log     │
│  ├ sequencer.py      — drives per-leg placement, partial-fill│
│  │                    tracking, reprices                     │
│  ├ residual_planner.py — residual-BAG builder (§7.3)         │
│  ├ protect.py        — TP attach + Risk-Alert registration   │
│  ├ quote_guard.py    — fresh-quote / crossed-quote gates     │
│  ├ rehydrate.py      — restart reconciler (exec-first)       │
│  └ store.py          — DuckDB-backed session store           │
│         data/leg_wizard.duckdb                               │
│                                                              │
│  New sibling handler:                                        │
│  src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py    │
│    (NOT a change to exit_orders.py — it keeps its current    │
│     PENDING_MANUAL trade_log.json responsibility intact)     │
│                                                              │
│  Extended (required before V1 ships):                        │
│  src/xenon/execution/naked_short_audit.py                    │
│    + wizard-session awareness (tag-based skip + per-leg      │
│      long-option coverage check in position ledger)          │
└─────│────────────────────────────────────────────────────────┘
      │
      ├──► ib_place_order.py (single-leg + BAG)
      ├──► ib_order_manage.py (modify / cancel)
      ├──► IB whatIf (jade-lizard margin preview)
      ├──► IB executionDetails / positions (rehydrate.py)
      └──► IB Gateway
```

Entry point: **modal launched from `OrderTab`** alongside the existing
`ComboOrderForm`. No standalone route.

## 5. Data model

```python
# src/xenon/execution/leg_wizard/models.py

class LegAttempt(BaseModel):
    attempt_id: str             # UUID; part of idempotency key
    ib_order_id: str | None
    perm_id: str | None
    target_price: Decimal
    price_basis: Literal["BID", "MID", "ASK", "CUSTOM"]
    submitted_at: datetime
    filled_qty: int             # 0..requested_for_attempt
    avg_fill_price: Decimal | None
    terminal_state: Literal[
        "PENDING", "WORKING", "FILLED", "PARTIALLY_FILLED",
        "CANCELLED", "REJECTED"
    ]
    ib_reject_code: int | None  # e.g. 201
    ib_reject_text: str | None

class LegPlan(BaseModel):
    leg_id: str
    order_index: int
    contract: OptionContract
    action: Literal["BUY", "SELL"]
    ratio: int
    requested_qty: int          # = ratio * structure_count
    filled_qty: int             # SUM across attempts
    remaining_qty: int          # = requested_qty - filled_qty
    avg_fill_price: Decimal | None
    risk_class: Literal["RISK_REDUCING", "RISK_INCREASING"]
    max_permitted_qty_now: int  # ≤ currently-hedged qty if RISK_INCREASING
    attempts: list[LegAttempt]

class BracketConfig(BaseModel):
    # TP policy is per-structure-class (§8.1)
    tp_enabled: bool
    tp_pct_of_max_profit: Decimal | None   # fixed-risk spreads only
    tp_ib_order_id: str | None             # after attach
    # Risk Alert (formerly "SL"); does NOT guarantee loss containment
    alert_enabled: bool
    alert_net_mid_threshold: Decimal | None  # signed
    alert_virtual_id: str | None             # wizard_stop_monitor id
    time_stop_dte: int | None

class SessionState(str, Enum):
    PLANNED             = "planned"
    IN_PROGRESS         = "in_progress"
    STALLED             = "stalled"
    REJECTED            = "rejected"           # IB hard-reject path
    PROTECTION_PENDING  = "protection_pending" # last fill done, bracket attaching
    PROTECTION_FAILED   = "protection_failed"  # bracket attach retries exhausted
    PROTECTED           = "protected"          # TP armed + Risk Alert armed
    ABORTED             = "aborted"            # no legs filled
    ROLLED_BACK         = "rolled_back"        # filled legs unwound via tiered limits

class WizardSession(BaseModel):
    session_id: str
    ticker: str
    structure_name: str                  # catalog "name" field, NOT "id"
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    has_short_leg: bool
    intent: Literal["OPEN", "CLOSE"]
    mode: Literal["A"]                   # V1 only
    sequencing: Literal["SAFETY", "LIQUIDITY"]
    net_target: Decimal                  # signed: debit +, credit −
    max_slippage: Decimal
    legs: list[LegPlan]
    brackets: BracketConfig | None
    state: SessionState
    events: list[SessionEvent]           # append-only audit log
    tag: str                             # stable tag for IB orders + audit skip
    created_at: datetime
    updated_at: datetime
```

**Idempotency key for `/place-next`**: `(session_id, leg_id, attempt_id)`
where `attempt_id` is supplied by the client on each call. Server rejects
duplicate `attempt_id`s; retries with new `attempt_id` are explicit.

## 6. Planner (planner.py)

### 6.1 Leg ordering (γ rule)

```
plan(structure_name, intent, legs, prices, sequencing=SAFETY) -> Plan:
    # Step 1. Classify each leg vs post-fill position:
    #   RISK_REDUCING   = Gate-4 and max-loss posture ≥ pre-fill after this leg
    #   RISK_INCREASING = otherwise
    # Step 2. Structure-specific sequencing rules (§6.2)
    #         fall back to SAFETY-with-widest-bid-ask tie-breaker
    # Step 3. If sequencing == LIQUIDITY:
    #   REJECT if has_short_leg is True (Gate-4 safety; no naked window)
    # Step 4. Per-leg default target_price:
    #   BUY  -> MID, capped at ASK-1tick
    #   SELL -> MID, floored at BID+1tick
    # Step 5. Compute net_target (signed)
    # Step 6. Gate 1 check (max_gain / max_loss ≥ 2); require acknowledge
    #         if fails. Gate 4 server-side check on the *full* plan.
```

### 6.2 Structure-specific sequencing rules

Stored as a `sequencing_rules` block per structure in a new sibling file
`docs/trading/options-structures-sequencing.json` (keyed by catalog
`name`). Required for: BWB, iron condor, iron butterfly, jade lizard,
calendar, diagonal. Each rule specifies:

- Explicit leg order by role (e.g. BWB: `far_long_wing → near_long_wing →
short_1 → short_2`, with short-leg quantity capped by realized
  long-wing qty).
- Partial-fill policy: if long wing fills 7/10, short-leg submissions
  are capped at 7 until the wing fills more.

### 6.3 Server-side Gate-4 enforcement

Before `/place-next` releases an order to IB, a Gate-4 check runs against
**current live positions + all in-flight wizard leg fills**:

- Port `web/lib/nakedShortGuard.ts` logic into
  `src/xenon/execution/leg_wizard/gate4.py`.
- Treats long calls on the same underlying+expiry as valid cover for
  short calls (vertical coverage), in addition to stock.
- Treats long puts on same underlying+expiry as valid for short puts
  (cash-secured not required when a vertical put spread covers).

This is the authoritative guard. UI check remains for UX speed but is
advisory.

## 7. Execution state machine (sequencer.py)

### 7.1 Transitions

```
PLANNED ─confirm plan─▶ IN_PROGRESS (leg 0 placed at IB)
                            │
                            ├─ leg fills fully ─▶ next leg eligible
                            │                     (user click in Mode A)
                            │                     loop until remaining_qty==0
                            │                     on ALL legs
                            │                     ──▶ PROTECTION_PENDING
                            │
                            ├─ leg partially fills ─▶ re-plan:
                            │     risk-increasing legs' max_permitted_qty_now
                            │     is recomputed against currently-hedged qty;
                            │     wizard may proceed with CAPPED qty
                            │     instead of stalling
                            │
                            ├─ leg rejected by IB (201 / margin / contract)
                            │     ──▶ REJECTED
                            │     user must choose: retry-reprice | abort
                            │     (rollback if any legs filled)
                            │
                            ├─ leg working > T (default 60s) ─▶ STALLED
                            │     resolve via §7.2
                            │
                            └─ user aborts ─▶
                                 if any leg filled: ROLLED_BACK
                                   (tiered limit unwind, §7.4)
                                 else:            ABORTED

PROTECTION_PENDING ─attach_ok──▶ PROTECTED
                  ─retries exhausted──▶ PROTECTION_FAILED
                                         user notified to manually exit
```

### 7.2 Stall resolution menu

Shown when STALLED:

- **Retry at new price** — user-entered limit; creates new `LegAttempt`
  with fresh `attempt_id`.
- **Tiered-limit unwind & abort** — unwinds filled legs via progressive
  limit prices (§7.4). No market orders offered.
- **Residual BAG** — see §7.3.

### 7.3 Residual BAG (residual_planner.py)

When the user selects "convert remainder to BAG", the planner:

1. Reads current filled quantities per leg from the wizard session.
2. Computes `target_structure − filled = residual legs` (may differ from
   original plan in both quantity and composition).
3. Builds a fresh BAG with `Order.action=BUY` envelope and per-leg
   `ComboLeg.action` set per the Xenon convention (LONG→BUY, SHORT→SELL),
   **never flipping Order.action** to encode direction.
4. Submits via existing `xenon-ib-place-order --type combo`.
5. On fill: reconciles into the session as a single composite fill
   event; transitions to PROTECTION_PENDING.

### 7.4 Tiered-limit unwind (replaces the removed "market-close" option)

To unwind filled legs safely after abort or REJECTED:

1. Start at **current mid**.
2. If no fill in 20s, move to **mid ± 10%** (toward natural market).
3. If no fill in 40s, move to **mid ± 25%**.
4. If no fill in 60s, **prompt user** with current market + explicit
   "Accept unbounded slippage (market order)" button. Never auto-market.

All unwind orders run through Gate 4: closing a short leg while a long
wing remains is always allowed (reduces risk); closing the long wing
first while a short remains open is blocked.

## 8. Protection (protect.py)

### 8.1 Per-structure TP policy

| Structure class                                                 | TP policy                                                                                                                   |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Fixed-risk debit (long vertical, long butterfly, BWB)           | Broker GTC BAG limit at `entry_net + tp_pct × max_profit` (signed, tested both directions)                                  |
| Fixed-risk credit (short vertical, iron condor, iron butterfly) | Broker GTC BAG limit at `entry_net − tp_pct × max_profit` (signed)                                                          |
| **Calendar, diagonal**                                          | **Broker TP forbidden in V1** (max profit depends on IV/DTE, no closed-form). Alert-only at user-entered net-mid threshold. |
| **Jade lizard**                                                 | Broker GTC BAG limit at configured % of credit received.                                                                    |

Closing BAGs always use `Order.action=BUY` envelope with per-leg
`ComboLeg.action` inverted at the leg level (LONG entry leg → SELL in
close BAG → encoded as `ComboLeg.action=SELL` because the catalog action
flips). **Never** encode direction via `Order.action=SELL` — per
`src/xenon/CLAUDE.md` this reverses the spread and triggers known
error-201 bugs.

Regression: unit test + browser test for every structure's entry→close
BAG pair, verifying the IB payload matches expected leg actions and that
sign is preserved end-to-end.

### 8.2 Risk Alert (formerly "SL")

**Naming:** this feature is a _Risk Alert → Assisted Exit_, not a
stop-loss. Loss containment is **not guaranteed** in V1. UI and
documentation must say this explicitly. Users seeking hard SL use IB's
broker-side stop on their own (out of scope).

**Behaviour:**

1. On transition to PROTECTED, a row is written to the new
   `wizard_stop_monitor.py` handler (NOT `exit_orders.py`).
2. Handler polls structure net-mid via the existing `quote_guard.py`
   freshness rules (§9).
3. When threshold is breached for N-of-M consecutive samples with all
   quotes fresh:
   - A desktop + email notification fires.
   - A new close-intent `WizardSession` is auto-created in PLANNED state
     with a pre-populated legging plan.
   - User must confirm to execute. If no confirmation within
     `risk_alert_ack_timeout` (default 60s), a **badge escalates**;
     there is no auto-execution.

### 8.3 Atomic protection

`PROTECTION_PENDING` is entered atomically with final fill. Transition
to `PROTECTED` requires:

- TP IB order id present AND acknowledged by IB open-orders.
- Risk Alert row persisted and picked up by the monitor loop.

If any sub-step fails, retry with exponential backoff (max 5 attempts
over 30s), then move to `PROTECTION_FAILED`. A banner prompts the user
to manually register exits; the session remains PROTECTION_FAILED until
user resolves.

### 8.4 OCA link (Xenon-managed)

- `tp_fill_watcher` (polls `ib_sync`) cancels the Risk Alert row when TP
  fills.
- `wizard_stop_monitor` cancels the TP IB order when Risk Alert fires
  and the user confirms the unwind.

## 9. Quote freshness (quote_guard.py)

Any net-mid evaluation (Risk Alert trigger, stall detection, TP
suggested default) must pass:

- Per-leg quote `ts` within `max_age_ms` (default 1500ms during market
  hours).
- Non-zero `bid_size` and `ask_size`.
- No crossed book (`bid <= ask`).
- No `NaN` / null fields.
- Cross-quote net-mid (BUY legs use ASK, SELL legs use BID — **not**
  mid-mid) per `web/CLAUDE.md` convention.

Risk Alert additionally requires **N-of-M** consecutive samples
(default 3-of-5) above threshold before firing.

## 10. API surface

| Method | Path                                      | Purpose                                                                                                                            |
| ------ | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| POST   | `/api/wizard/plan`                        | Build a plan; no side effects. Server-side Gate checks applied.                                                                    |
| POST   | `/api/wizard/sessions`                    | Persist plan; move to `PLANNED`.                                                                                                   |
| POST   | `/api/wizard/sessions/{id}/place-next`    | Body: `{leg_id, attempt_id, target_price}`. Idempotent on `(session_id, leg_id, attempt_id)`. Returns IB order id + session state. |
| POST   | `/api/wizard/sessions/{id}/resolve-stall` | `{action: "retry"\|"abort"\|"bag", price?, attempt_id?}`.                                                                          |
| POST   | `/api/wizard/sessions/{id}/abort`         | Cancel open legs; triggers tiered-limit unwind for filled legs.                                                                    |
| POST   | `/api/wizard/sessions/{id}/protect`       | Attach TP + Risk Alert after final fill. Triggered automatically by sequencer; exposed for manual retry from PROTECTION_FAILED.    |
| GET    | `/api/wizard/sessions/{id}`               | Full session state.                                                                                                                |
| GET    | `/api/wizard/stream?session_id=…`         | SSE: session events.                                                                                                               |
| POST   | `/api/wizard/sessions/{id}/reconcile`     | Force rehydrate from IB executions (used by restart reconciler + manual debug).                                                    |

All authed via Clerk bearer + `ALLOWED_USER_IDS`.

## 11. Gates, guardrails & pre-V1 prerequisites

### 11.1 Pre-V1 required extensions

These must land **before P1**:

1. **`naked_short_audit.py` extended** to:
   - Recognise a `leg_wizard` order tag and skip orders under active
     wizard protection (they're governed by server-side Gate 4).
   - For non-tagged OPT orders, add long-option coverage check
     (vertical pairing), not just stock coverage.
   - Regression: `scripts/tests/test_naked_short_audit.py` expanded with
     wizard-tagged short call + long call position → not flagged.

### 11.2 Runtime gates

- **Gate 1 (convexity)**: planner rejects `max_gain / max_loss < 2`
  unless user acknowledges; written into `events`.
- **Gate 4 (no naked)**: server-side `gate4.py` evaluates full plan
  at `/plan` and every `/place-next`. LIQUIDITY sequencing rejected
  when `has_short_leg=True`.
- **Jade-lizard cash-secured check**: IB `whatIf` preview on the
  short-put leg immediately before each submission; compare against
  current `availableFunds` and `excessLiquidity`; persist the preview
  in `events`. Re-check before every subsequent leg (margin shifts on
  prior fills).
- **Slippage cap**: realized + expected > `max_slippage` →
  auto-`STALLED`.
- **Market hours**: session creation blocked outside 9:30–16:00 ET
  weekdays unless `user_initiated=True` on the plan call (mirrors
  `UwAnalyzeCache`).

## 12. Rehydration on FastAPI restart (rehydrate.py)

On boot, for every session in `IN_PROGRESS`, `STALLED`,
`PROTECTION_PENDING`, or `PROTECTED`:

1. Fetch IB open orders **and** `executionDetails` **and** current
   positions — three independent sources.
2. Reconcile per-leg fill state against **executions first**, not
   from open-order disappearance (disappearance can mean fill or
   cancel; executions are authoritative).
3. Re-register wizard_stop_monitor rows for PROTECTED sessions.
4. For PROTECTION_PENDING: attempt bracket attach again; move to
   PROTECTION_FAILED after retries exhausted.
5. Never infer state from a single source; log any source
   disagreement as a SessionEvent.

## 13. Frontend surface

Entry: "Place via Wizard" button inside `ComboOrderForm`, visible only
when staged combo matches a V1 structure.

Steps:

1. **Structure confirm** — detected name from catalog, leg pills, signed
   net mid, **risk badge**. Gate 1 + Gate 4 banner.
2. **Plan review** — ordered leg list with per-leg target price
   (BID/MID/ASK quick-fill), net target, max-slippage budget, risk
   acknowledgement checkbox when **High**.
3. **Bracket setup (opens only)** — TP policy dropdown (disabled for
   calendar/diagonal), Risk Alert threshold, time-stop DTE (default
   none). Copy clearly states Risk Alert is **assisted-exit**, not a
   stop-loss.
4. **Execute** — live leg list; next-to-place highlighted. Placed legs
   show IB order id, filled_qty/requested_qty, avg fill price.
   SSE-driven.
5. **Stalled / Rejected view** — menu from §7.2. Market orders are not
   listed; tiered-limit unwind is the default safe path.
6. **Result** — net achieved vs target, total slippage, session id,
   state (PROTECTED / PROTECTION_FAILED), links to TP + Risk Alert.

UI must tolerate SSE disconnect (badge + retry) and browser tab close
(session state is server-authoritative; reopen resumes from current
state).

All colors via brand tokens; panels ≤4px radius (per `brand/CLAUDE.md`).

## 14. Testing

### 14.1 Mandatory regressions

- Partial 1/N long-wing fill → short-leg submission capped at 1.
- Short-leg REJECTED (IB 201) after long wing filled → state machine
  reaches REJECTED; tiered-limit unwind offered; Gate 4 passes on
  unwind.
- Restart mid-IN_PROGRESS → rehydrate via executions (not
  open-order disappearance).
- Browser tab close + reopen mid-execute → UI resumes from server state.
- Duplicate `place-next` with same `(session, leg, attempt_id)` →
  second request is a no-op, not a duplicate IB submission.
- Stale-leg quote causing false-positive Risk Alert threshold breach →
  quote_guard suppresses until N-of-M fresh samples.
- TP attach failure (IB reject) → PROTECTION_PENDING → retry → eventual
  PROTECTION_FAILED → manual protect button works.
- Residual BAG after 1/4 iron condor fill → payload has correct
  residual legs + ratios, Order.action=BUY, ComboLeg.action LONG→BUY
  SHORT→SELL.
- Sign preservation through TP limit price for credit structures
  (regression against `Math.abs` pipeline).
- Jade-lizard whatIf rejected by insufficient excess liquidity →
  short-put leg refused; planner state stays pre-placement.
- naked_short_audit does NOT cancel a wizard-tagged short call when
  its long-call cover is already filled.

### 14.2 Test surfaces

- **Unit (Python)**: planner, sequencer, residual_planner, gate4,
  quote_guard, protect.
- **Integration (Python)**: `scripts/tests/test_leg_wizard/` with
  `FakeIBClient`; simulates partial fills, rejects, stalls, restart
  recovery, whatIf flows.
- **Frontend unit (Vitest)**: `useWizardSession` reducer, modal state.
- **E2E (Playwright)**: full opens (vertical, iron condor, jade
  lizard with ack), full closes, stall→residual-BAG, REJECTED unwind,
  Risk Alert escalation.
- **Coverage target**: 95% per Xenon policy.

## 15. Ship plan (phased)

| Phase  | Deliverable                                                                                                           | Blocking prereqs |
| ------ | --------------------------------------------------------------------------------------------------------------------- | ---------------- |
| **P0** | Extend `naked_short_audit.py` (§11.1) with wizard-tag skip + long-option coverage. Ship to prod ahead of wizard code. | none             |
| **P1** | planner + gate4 + session store + `FakeIBClient` integration tests; no IB, no UI.                                     | P0               |
| **P2** | FastAPI routes + SSE + IB integration behind `XENON_API_TEST_MODE`; paper-only.                                       | P1               |
| **P3** | `WizardModal` over OrderTab. Opens only. No brackets yet.                                                             | P2               |
| **P4** | Protection: TP broker GTC + `wizard_stop_monitor.py` handler + atomic PROTECTION_PENDING/PROTECTED.                   | P3               |
| **P5** | Close flow (`intent=CLOSE`), residual-BAG builder, REJECTED / tiered-unwind paths.                                    | P4               |
| **P6** | Graduation: mode B auto-anchor, auto-chase, auto-convert-BAG, true auto-SL — all behind flags, default off.           | P5               |

## 16. Open questions / deferred

- Multi-account support: V1 = default IB account only.
- Exchange pinning per leg: V1 always SMART; revisit if specific
  exchanges improve individual leg fills materially.
- Roll flow: out of V1; schema supports future `intent="ROLL"` without
  breakage.
- True auto-SL (daemon-enforced bounded-slippage unwind): P6 graduation,
  requires product-side policy decision on whose loss this is when a
  bounded unwind fails.
