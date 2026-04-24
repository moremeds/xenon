# Combo-First Spread Execution Wizard — Design

Date: 2026-04-20
Status: Reviewed v0.4 (combo-first revision on 2026-04-24)
Depends on: completion of `docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md`
(phases F0–F7, shipped via PR #25/#27/#28/#29). The archived master plan at
`docs/superpowers/archive/plans/2026-04-20-order-execution-foundation-master.md`
is historical context only; active implementation is driven by the current
execution plan for this spec.

Burn-in gate: waived on 2026-04-23. Foundation observability
(`REHYDRATE_RECONCILED` / `REHYDRATE_UNCERTAIN`, `orders_events`,
`naked_short_audit`) remains active and is reused by this design.

Related code: `src/xenon/execution/`, `src/xenon/api/server.py`,
`src/xenon/api/routes/`, `src/xenon/monitor_daemon/run.py`,
`src/xenon/monitor_daemon/handlers/`, `web/lib/order/`,
`web/lib/optionsChainUtils.ts`, `web/components/ticker-detail/OrderTab.tsx`,
`web/components/ticker-detail/OptionsChainTab.tsx`,
`web/app/api/orders/place/route.ts`, `web/app/api/orders/modify/route.ts`,
`docs/trading/options-structures.json`, `src/xenon/execution/naked_short_audit.py`,
`web/lib/nakedShortGuard.ts`

Changelog v0.3 → v0.4: replaced the leg-by-leg V1 premise with a
combo-first execution model for defined-risk spreads; repositioned the popup as
a combo execution assistant instead of a per-leg sequencer; removed leg-by-leg
placement from the default path; aligned execution to natural/mid laddering;
kept Xenon’s mandatory BAG semantics (preserve sign and per-leg actions; do not
flip BAG structure by net debit/credit); retained the popup modal and compact
resume strip as the UI shape.

## 1. Purpose & non-goals

**Purpose.** Execute defined-risk spreads as a **single combo/BAG order**
instead of sequencing legs individually, reducing legging risk while preserving
existing Xenon guarantees around signed pricing, Gate 4, idempotency,
rehydration, and operator-visible failure handling.

The primary risk reduction is straightforward: a supported spread should be
priced, submitted, and modified as one structure so the operator is not exposed
to the default V1 failure mode of getting one leg filled while the rest of the
position is still unhedged.

**Non-goals.**

- Not a replacement for single-leg or stock order entry.
- Not a scanner or idea generator.
- Not a naked-short enabler. Gate 4 remains authoritative and is enforced
  server-side.
- Not a leg-by-leg default workflow for defined-risk spreads.
- Not a guarantee of loss containment. Risk Alert remains assisted-exit logic.
- Not a rewrite of Xenon’s existing BAG semantics.

## 2. Core execution rule

### 2.1 Default rule

For **defined-risk spreads**, Xenon should:

- build the trade as **one combo/BAG order**
- show **natural / mid / ladder** guidance
- submit and modify the **combo as a whole**
- avoid leg-by-leg sequencing unless the user explicitly chooses an advanced
  legging mode in a future phase

This applies to both **opening** and **closing** defined-risk spreads.

The UI and API should treat combo placement as the lower-risk default for these
structures. If a structure is eligible for combo-first V1 support, Xenon should
not route the operator into a sequenced-leg path.

### 2.2 Xenon BAG semantics remain mandatory

The external trading premise is directionally correct on risk, but Xenon must
still obey the project’s mandatory combo-order rules:

- Do **not** derive BAG `Order.action` from net debit vs net credit.
- Preserve the intended per-leg `BUY` / `SELL` actions.
- Preserve signed combo prices end-to-end.
- Never use `Math.abs()` or equivalent to erase credit/debit sign.

For Xenon order payloads, the BAG envelope must continue to follow the existing
house convention already encoded in `web/lib/optionsChainUtils.ts`,
`web/components/ticker-detail/OrderTab.tsx`, and the order-route tests.

The project-wide BAG guardrails in `CLAUDE.md` (src/xenon) are authoritative and
apply to the wizard without modification:

1. Never map combo `Order.action` from debit vs credit — envelope stays `BUY`,
   per-leg actions carry the structure.
2. On any **single-leg → combo** transition in the parent order surface, the
   stale top-level manual net price must be cleared and recomputed from the
   normalized combo quote. The wizard inherits this rule for any single-to-combo
   handoff (e.g. operator opens the popup from a single-leg builder state).
3. Required regressions for combo-entry bugs: a unit test for combo
   action/ratio/net-price semantics **and** a browser test for displayed combo
   net price + submitted payload.

**Implication:** this design is **combo-first**, but it is **not** a naive
adoption of generic IB ticket examples that would flip the spread or re-create
the known BAG reversal bug.

## 3. Scope

| In V1                                                                         | Out of V1                                                                           |
| ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Opens + closes for defined-risk spreads as one combo order                    | Leg-by-leg default execution                                                        |
| Structures: verticals, iron condors, iron butterflies, long butterflies, BWBs | Standalone legging workflows for standard spreads                                   |
| Combo execution popup with natural/mid ladder guidance                        | Dedicated wizard page                                                               |
| Combo modify-in-place repricing workflow                                      | Drawer / slide-over UI                                                              |
| Protection for supported defined-risk combo structures                        | Calendars / diagonals / jade lizard until combo-first rules are separately reviewed |
| Compact parent-session strip with `Resume Wizard`                             | Auto-chase / unattended repricing                                                   |
| Dry-run / paper mode via `XENON_API_TEST_MODE`                                | Auto-legging or smart leg sequencing                                                |

## 4. Pricing framework

### 4.1 Displayed prices

The popup must present three concepts clearly:

- **Natural**: executable anchor now
- **Mid**: fair negotiation start
- **Ladder**: controlled movement from mid toward natural

### 4.2 Natural quote math

Natural pricing must use cross-fields, not mid-mid:

```text
BUY combo  -> pay ASK on BUY legs, receive BID on SELL legs
SELL combo -> receive BID on BUY legs, pay ASK on SELL legs
```

This must match the existing combo quote semantics already enforced in
`computeNetOptionQuote()` and the project rules.

### 4.3 Ladder behavior

For liquid defined-risk spreads:

- Start at `mid` by default when the quoted width is reasonable.
- Walk toward `natural` in small increments.
- Suggested step sizes:
  - many equity spreads: `$0.02` to `$0.05`
  - wider/index products such as SPX: `$0.05` to `$0.10`

Execution ladder is operator-assisted:

- `MID`
- `MID ± step`
- `MID ± 2 * step`
- `NATURAL`

The actual sign/direction of the adjustment depends on whether the operator is
trying to receive more credit or pay less debit, but the UI should present this
in combo terms, not in per-leg math.

## 5. Architecture

```text
Frontend
  OptionsChainTab > OrderBuilder
  OrderTab > ComboOrderForm
    -> "Place via Wizard" opens popup modal
    -> compact session strip shows status/resume outside the modal
    -> Next.js /api/wizard/* proxies + SSE

FastAPI
  src/xenon/api/routes/wizard.py
    -> plan / sessions / submit / reprice / abort / protect / stream / reconcile
  mounted from src/xenon/api/server.py

Execution core
  src/xenon/execution/combo_wizard/
    planner.py
    combo_quotes.py
    session.py
    protect.py
    rehydrate.py
    store.py

Reused foundation
  orders_store.py
  quote_guard.py
  single_leg_rehydrate.py
  preflight.py
  ib_place_order.py
  ib_order_manage.py
```

### 5.1 Reuse, not fork

- Persist wizard session state in the same `data/orders.duckdb` file used by
  `orders_store`.
- Reuse `orders_submissions` / `orders_events` for actual combo submission and
  modify flows.
- Wizard submit must reuse the **same place-order code path and semantics** as
  `/orders/place` with `type: "combo"` (today routed to
  `src/xenon/execution/ib_place_order.py`, which already handles BAG +
  `NonGuaranteed=1`). Wizard reprice must reuse the **same modify-order code
  path and semantics** as `/orders/modify` (`ib_order_manage.py`, which
  re-applies `smartComboRoutingParams` for BAG modifies). This is a reuse-path
  requirement, not a loopback-HTTP requirement inside FastAPI. The wizard
  module MUST NOT edit `ib_place_order.py` or `ib_order_manage.py` — both
  files are shipped, hardened, and combo-aware today. Any per-leg construction
  logic lives behind them, not in `combo_wizard/`.
- Reuse `single_leg_rehydrate.py` decision logic where attempt/order-level
  reconcile semantics overlap, but combo rehydrate has a **BAG-specific**
  branch: IB reports per-leg executions under one parent `permId`, so
  reconciliation must sum executions against the combo attempt, not compare
  a single fill record to a single contract.
- Quote math: `web/lib/optionsChainUtils.ts::computeNetOptionQuote` remains the
  canonical signed-combo-quote source. The Python `combo_quotes.py` in the
  wizard is a **mirror** for server-side planning, and must carry a parity
  test that asserts identical outputs to the TS implementation for a fixed
  fixture set.

### 5.2 No V1 leg sequencer

The prior `place-next` per-leg state machine is removed from the default V1
design. V1 tracks a **combo order session**, not a list of released legs.

## 6. Data model

```python
class ComboAttempt(BaseModel):
    attempt_id: str
    client_attempt_id: str
    ib_order_id: str | None
    perm_id: str | None
    intent: Literal["OPEN", "CLOSE"]
    target_price: Decimal
    price_basis: Literal["NATURAL", "MID", "STEP", "CUSTOM"]
    ladder_step: Decimal | None
    submitted_at: datetime
    terminal_state: Literal[
        "PENDING", "WORKING", "FILLED", "PARTIALLY_FILLED",
        "CANCELLED", "REJECTED"
    ]
    filled_qty: int
    avg_fill_price: Decimal | None
    ib_reject_code: int | None
    ib_reject_text: str | None


class BracketConfig(BaseModel):
    tp_enabled: bool
    tp_target_price: Decimal | None
    tp_ib_order_id: str | None
    alert_enabled: bool
    alert_net_mid_threshold: Decimal | None
    alert_virtual_id: str | None
    time_stop_dte: int | None


class SessionState(str, Enum):
    PLANNED = "planned"
    SUBMITTING = "submitting"
    WORKING = "working"
    REPRICE_PENDING = "reprice_pending"
    STALLED = "stalled"
    FILLED = "filled"
    REJECTED = "rejected"
    PROTECTION_PENDING = "protection_pending"
    PROTECTION_FAILED = "protection_failed"
    PROTECTED = "protected"
    ABORTED = "aborted"


class WizardSession(BaseModel):
    session_id: str
    ticker: str
    structure_name: str
    risk_level: Literal["LOW", "MEDIUM"]
    intent: Literal["OPEN", "CLOSE"]
    mode: Literal["COMBO"]
    net_target: Decimal
    ladder_step: Decimal
    natural_price: Decimal | None
    mid_price: Decimal | None
    best_price: Decimal | None
    attempts: list[ComboAttempt]
    brackets: BracketConfig | None
    state: SessionState
    created_at: datetime
    updated_at: datetime
```

Persistence shape:

- `wizard_sessions`
- `wizard_combo_attempts`
- `wizard_session_events`
- `wizard_protection`

All live in `data/orders.duckdb`, not a parallel DB.

## 7. Planner

The planner no longer computes a leg release order for V1. It now computes:

- supported structure classification
- operator-facing risk level
- signed `natural`, `mid`, and ladder anchors
- recommended default ladder step
- whether the structure is eligible for V1 combo-first execution

If a structure is **not** in the defined-risk combo-first set, the wizard must
not silently fall back to legging. It should block with explicit copy such as:

`This structure is not in combo-first V1. Use the standard order form or wait for advanced mode.`

## 8. Session state machine

```text
PLANNED
  -> SUBMITTING
  -> WORKING
     -> FILLED
     -> REPRICE_PENDING
     -> STALLED
     -> REJECTED
     -> ABORTED

FILLED
  -> PROTECTION_PENDING
  -> PROTECTED
  -> PROTECTION_FAILED
```

State meanings:

- `PLANNED`: plan exists, no live combo submitted
- `SUBMITTING`: combo order being placed through shared order path
- `WORKING`: combo live at IB
- `REPRICE_PENDING`: operator requested a ladder step / modify
- `STALLED`: combo has not filled within operator-defined tolerance
- `REJECTED`: combo was rejected
- `FILLED`: combo entry/exit completed
- `PROTECTION_PENDING`: protection workflow running after fill
- `PROTECTED`: TP + Risk Alert armed where applicable
- `PROTECTION_FAILED`: operator must intervene
- `ABORTED`: combo cancelled / abandoned without a filled structure change

## 9. Protection

### 9.1 Supported V1 protection

For supported defined-risk spreads:

- Attach broker-side combo TP where the payoff definition is stable enough.
- Attach Risk Alert assisted-exit where operator confirmation is still required.

### 9.2 Risk Alert naming

This remains a _Risk Alert → Assisted Exit_, not a stop-loss. The popup must
say that explicitly.

## 10. Quote freshness & combo math

Any wizard price suggestion or Risk Alert threshold check must require:

- non-crossed quotes
- non-zero bid/ask sizes
- no null / NaN values
- cross-field combo math
- fresh quote timestamps

Implementation split:

- `quote_guard.py` remains authoritative for single-order submission gates
  already used by the existing order APIs.
- `combo_quotes.py` handles structure-level natural/mid math and freshness
  decisions for the popup session model.

## 11. API surface

### 11.1 FastAPI

| Method | Path                              | Purpose                                                 |
| ------ | --------------------------------- | ------------------------------------------------------- |
| POST   | `/wizard/plan`                    | Build combo-first plan with natural/mid/ladder guidance |
| POST   | `/wizard/sessions`                | Persist session in `PLANNED`                            |
| POST   | `/wizard/sessions/{id}/submit`    | Submit combo order through shared combo path            |
| POST   | `/wizard/sessions/{id}/reprice`   | Modify combo order to next/selected ladder price        |
| POST   | `/wizard/sessions/{id}/abort`     | Cancel combo order and close session flow               |
| POST   | `/wizard/sessions/{id}/protect`   | Attach TP + Risk Alert after fill                       |
| GET    | `/wizard/sessions/{id}`           | Fetch session snapshot                                  |
| GET    | `/wizard/stream?session_id=…`     | SSE session updates                                     |
| POST   | `/wizard/sessions/{id}/reconcile` | Force reconcile from IB state                           |

### 11.2 Next.js proxy

Mirror the above under `web/app/api/wizard/…` using the same `xenonFetch()`
and SSE proxy pattern Xenon already uses.

## 12. Gates & guardrails

- **Gate 1** remains server-authoritative.
- **Gate 4** remains server-authoritative.
- **Signed combo pricing** remains mandatory.
- **Combo entry/exit must use BAG as a whole**, not manual legging, for V1
  supported defined-risk spreads.
- **Market hours**: actual combo submission / repricing must respect the same
  option-tradeability window enforced elsewhere in the execution stack.
- **Advanced legging mode** is explicitly out of V1 scope.

## 13. Rehydration

On restart, for every `SUBMITTING`, `WORKING`, `REPRICE_PENDING`,
`PROTECTION_PENDING`, or `PROTECTED` session:

1. Fetch IB open orders, executions, and positions.
2. Reconcile the combo order attempt against executions first. Because IB
   reports combo fills as **per-leg executions sharing one parent `permId`**,
   reconcile by grouping executions on the attempt's `permId` and checking
   that every leg reached the expected ratio — do not treat a single-leg
   execution row as a combo fill.
3. Re-register Risk Alert rows for protected sessions.
4. Retry unfinished protection attachment.
5. Log any disagreement as a session event.

Paper-first gate: modify and abort behavior for BAG combos MUST be verified
against an IB paper account before any wizard modify/abort code lands on
master. This follows the project rule recorded in
`feedback_broker_bugs_paper_first` — broker modify/cancel bugs are diagnosed
against paper, never live money.

## 14. Frontend surface

### 14.1 Entry points

- `OptionsChainTab > OrderBuilder`: primary OPEN flow
- `OrderTab > ComboOrderForm`: OPEN/CLOSE flow for existing combo positions

### 14.2 Popup modal requirement

- The wizard is an **in-app popup modal dialog**.
- It is **not** a page route.
- It is **not** a drawer / slide-over.
- The underlying ticker page remains visible beneath the scrim.

### 14.3 Parent session strip

When a wizard session exists, show a compact status strip in the parent order
surface:

- `WIZARD SESSION`
- structure / state / current price anchor
- last event
- `Resume Wizard`
- `Abort` when valid

This strip is monitor/resume UI only. The workflow itself remains in the popup.

### 14.4 Visual spec

**Modal box**

- Width: `min(960px, calc(100vw - 32px))`
- Max height: `min(820px, calc(100vh - 48px))`
- Background: `bg.panel`
- Border: `1px solid line.grid`
- Radius: `4px`
- Shadow: none

**Header rail**

- `COMBO WIZARD`
- `MODE COMBO · NATURAL/MID LADDER · SERVER AUTHORITY · SESSION ID`
- IBM Plex Mono telemetry styling

**Body**

- Step strip:
  `STRUCTURE · PRICE PLAN · EXECUTE · PROTECT · RESULT`
- Desktop:
  - left: main workflow
  - right: telemetry rail
- Mobile:
  - single-column collapse

**Main workflow content**

- `Structure`: structure, leg pills, signed natural/mid, risk badge, gate rows
- `Price Plan`: natural, mid, ladder buttons, chosen target
- `Execute`: current combo state, working price, modify controls
- `Protect`: TP + Risk Alert configuration
- `Result`: achieved fill, slippage, final state

**Telemetry rail**

- ticker
- structure
- session state
- current working price
- quote freshness
- last event
- signed net target / achieved

**Footer rail**

- left: `Abort`, `Back`, `Reprice`
- right: `Submit Combo`, `Move Toward Natural`, `Confirm Protection`, `Done`
- footer remains visible while the body scrolls

### 14.5 Page alignment

This UI must align with the overall Xenon page design:

- use `docs/reference/brand-identity.md`
- use Xenon panel surfaces, hairline borders, and telemetry rails
- use Inter + IBM Plex Mono
- no soft rounded consumer modal styling
- no heavy shadows
- no decorative gradients

## 15. Testing

### 15.1 Mandatory regressions

- Combo natural quote math uses cross-fields, not mid-mid
- Signed credit/debit is preserved through popup, routes, and payloads
- Combo submit uses the whole BAG order, not per-leg release
- Reprice modifies the live combo order instead of replacing it with manual legs
- Restart rehydrates combo session from executions/open orders
- Risk Alert copy says assisted-exit, not stop-loss
- Popup resumes server-authoritative session after close/reopen
- Parent session strip appears in both Order Builder and Order Tab

### 15.2 Test surfaces

- Python unit: planner, combo quotes, protection, rehydrate
- Python API: wizard routes
- Frontend unit: modal layout, session strip, session hook
- Playwright: popup open, combo submit, reprice ladder, resume flow, Risk Alert

## 16. Ship plan

| Phase | Deliverable                                                          |
| ----- | -------------------------------------------------------------------- |
| P1    | combo-first planner, store, and quote math                           |
| P2    | FastAPI + Next.js wizard session routes wired to combo submit/modify |
| P3    | popup modal + parent session strip in both order surfaces            |
| P4    | protection, restart reconcile, daemon registration                   |
| P5    | release verification and operator docs                               |

## 17. Deferred

- Advanced legging mode
- Calendars / diagonals / jade lizard combo-first review
- Auto-chase
- Dedicated performance analytics for ladder effectiveness

## 18. Implementation status

Tasks 1–5.5 shipped in PR #46 (feat/combo-wizard) on 2026-04-25. Pre-merge gates: live dry-run per §13, plus protect.py except narrowing, combo_quote_source TTL + subscription reuse, and the two Playwright test.fixme flips.
