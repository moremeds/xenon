# Combo Wizard — Operator Manual

Audience: Xenon operators who have never touched the wizard. Last updated 2026-04-25.

## 1. What the wizard is

Combo-first spread execution for defined-risk spreads. The wizard submits the whole structure as a single IB BAG order so there is no legging risk between entry legs, runs a laddered natural/mid pricing loop to probe for a fill, and runs a post-fill protection pipeline (broker-side take-profit plus an operator-confirmed Risk Alert). Session state lives in `data/orders.duckdb` under the `wizard_*` tables; actual submits, modifies, and rehydrate flow through the existing `orders_store` + `ib_place_order.py` + `ib_order_manage.py` stack.

## 2. When to use it

V1 supports defined-risk combos only:

- Verticals (bull call, bear put, bull put, bear call)
- Long iron condors and long condors
- Long butterflies
- Jade lizards (call spread + cash-secured short put; the call spread covers the short call, the put is cash-secured)

Do **not** use the wizard for:

- Anything with an uncovered short call or uncovered short stock (Gate 4 will block you)
- Ratio verticals (1x2, 1x3) — deferred until V2
- Advanced leg-at-a-time release — deferred until V2
- Calendars / diagonals — deferred (see spec §17)

## 3. How to open it

- **From the Options Chain tab** → build a spread in Order Builder → click **Open Wizard**.
- **From the Order tab** → open Combo Order Form → click **Open Wizard**.

The popup is the same modal in both surfaces; it is launched by `web/lib/useWizardLauncher.ts` and rendered by `web/components/ticker-detail/WizardModal.tsx`. A compact parent strip (`WizardSessionStrip.tsx`) appears above both surfaces when a session is active so you can reopen a running wizard at any time.

## 4. The flow

Five states, one sentence each:

- **PLANNED** — you confirmed the structure; the wizard has stored the session and computed natural/mid from live leg quotes, but nothing has been sent to IB.
- **SUBMITTING** — the BAG order has been handed to `/orders/place`; Xenon is waiting for IBKR to assign a permId.
- **WORKING** — the combo is live on the IB book; the ladder loop is probing at the current limit, and reprice clicks step the limit toward natural.
- **PROTECTED** — the combo filled; the take-profit BAG attached (if Gate 4 allows), and the Risk Alert (assisted-exit) is armed.
- **Terminal** — one of: `ABORTED` (operator canceled), `FILLED_AND_CLOSED` (TP took it off), `PROTECTION_PENDING` (fill went through but TP attach failed; the monitor daemon will re-drive), or `PROTECTION_REFUSED` / `PROTECTED-with-refused-TP` when Gate 4 blocked the TP but the Risk Alert is the operator's safety net.

## 5. Key buttons

- **Submit** — ships the BAG at the current limit. Safe when you have accepted the planner's structure and the quotes look non-crossed. The combo submit path is guarded (naked-short audit, quote freshness gate) before it reaches IB.
- **Reprice** — moves the limit one step toward natural on the ladder. Safe to press while WORKING. Modifies the live combo order rather than canceling + re-submitting, so permId / rehydrate continuity is preserved. See `src/xenon/execution/ib_order_manage.py`.
- **Abort** — cancels the live BAG. Safe any time before terminal; does not touch fills that already cleared. If a partial leg ever materialized (not expected for a BAG, but rehydrate guards against it), the combo is closed atomically and the residual is surfaced for manual handling.

## 6. Glossary

**BAG order** — IB's combo-order contract type (`secType = "BAG"`). All legs submit atomically under one `Order`. Per-leg `ComboLeg.action` encodes the structure (LONG leg = `BUY`, SHORT leg = `SELL`). `Order.action` is the envelope (`BUY` for open, `SELL` to close a long-debit combo — IB reverses the legs on the envelope). Never flip both per-leg action and envelope — the double reverse produces IB error 201. See `src/xenon/execution/combo_wizard/ib_adapter.py:190-310` and `src/xenon/CLAUDE.md §Combo / BAG Order Guardrails`.

**permId** — IBKR's server-assigned persistent order id. It survives reconnects, restarts, and the ib_insync client id rotation. Xenon keys all rehydrate reconciliation on permId. ib_insync seeds `Trade.order.permId = 0` client-side and only fills the real value once the broker's `openOrder` ack arrives, so the adapter polls briefly before returning. See `_wait_for_perm_id` at `src/xenon/execution/combo_wizard/ib_adapter.py:92-135`.

**Rehydrate** — on FastAPI boot, Xenon reconciles in-memory session state against three sources of truth: the orders DB, live IB open orders (`reqAllOpenOrders`), and live IB executions (`reqExecutions`). A crash or restart cannot lose an in-flight combo. BAG rehydrate specifically groups per-leg `Execution` rows by parent `permId` and only marks the session `FILLED` when every leg hit its `ratio × quantity`. See `src/xenon/execution/combo_wizard/rehydrate.py`.

**Natural vs mid** — natural is the cross-field combo price: BUY legs at ASK, SELL legs at BID. Submitting at natural gets you filled now at a worse price. Mid is the leg-by-leg average of bid and ask. Submitting at mid is patient. Signed: debits positive, credits negative, same rule everywhere. See `src/xenon/execution/combo_wizard/combo_quotes.py`.

**Ladder** — the reprice sequence. The wizard starts closer to mid and steps one ladder rung toward natural on each Reprice click. The loop terminates on fill or abort. See `src/xenon/execution/combo_wizard/planner.py`.

**Risk Alert → Assisted Exit** — Xenon's non-stop-loss protection mechanism. When a combo's signed net-mid breaches a stored threshold, Xenon notifies the operator to close manually. It is **NOT** an auto-stop; the operator confirms the exit. Spec §9.2 explicitly forbids calling this a stop-loss. The popup copy lives in one place — `protect.risk_alert_popup_copy()` — so the wording stays grep-able. The monitor handler polls PROTECTED sessions every 30s and never places an order: `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py`.

**TP attach** — the post-fill take-profit combo Xenon places automatically on structures where the payoff is stable. It is a separate BAG from the entry, submitted `Order.action = "SELL"` (to close a long-debit combo) with the structure's per-leg actions preserved. `protect.attach_protection` retries up to 3 times with 2s→4s→8s exponential backoff on transient failures. Terminal Gate-4-style refusals (e.g., IB error 201) short-circuit the retry loop and record a `PROTECTION_TP_REFUSED` event without waste — then the Risk Alert arms anyway so the operator keeps a safety net. If the attach fails on all three retries for a _transient_ reason, the session sits in `PROTECTION_PENDING` and the monitor daemon re-drives. See `src/xenon/execution/combo_wizard/protect.py:199-391`.

**Gate 4** — Xenon's naked-short guard. Every combo submit, every reprice, and every TP attach checks that short legs are covered: shares for short stock, longer calls at the same expiry for short calls, cash for short puts. Violations raise `NakedShortGuardError` and never reach the broker. The guard lives at both UI and API layers; the protect-path guard is in `protect._uncovered_short_calls`. Full allow/block table: `src/xenon/CLAUDE.md §Naked Short Protection`.

**Signed combo pricing** — debits are positive, credits are negative, everywhere. The UI, the DB `wizard_protection.tp_target_price` and `alert_net_mid_threshold` columns, and the order payload all preserve sign. There is no `Math.abs()` or `abs()` anywhere in the pipeline. This is how `_crossed(quote, threshold)` in the monitor stays one line.

**SSE** — the wizard modal gets session state updates via server-sent events, not polling. Exactly one stream per session — the subscription is hoisted to the parent in `web/lib/useWizardSession.ts` so the popup modal and the `WizardSessionStrip` share one connection instead of opening two.

## 7. Restart behavior

If you kill and restart FastAPI while a session is WORKING or PROTECTED, on boot the server fetches IB's open orders and executions and reconciles against the `wizard_sessions` + `wizard_protection` tables. Your session will reappear in the UI in whatever state the broker says it is in. See **Rehydrate** in the glossary and `src/xenon/execution/combo_wizard/rehydrate.py`.

## 8. Known gaps (as of 2026-04-25)

From the pre-merge plan for PR #46:

- Live dry-run per spec §13 — **pending**. This is the last merge gate.
- `protect.py` except narrowing — **shipped** (Item 2 in the follow-up; `NakedShortGuardError` short-circuits the retry loop instead of burning 14s on terminal rejects).
- Quote TTL env-tunable — **shipped** (`XENON_WIZARD_QUOTE_TTL_S`, default 30s).
- Subscription reuse in `combo_quote_source` — **shipped** (`_TickerCache` keys on conId; cleanup on terminal transitions cancels via `ib.ib.cancelMktData`).
- Two Playwright `test.fixme` flips — **remain**. See `web/tests/*wizard*` for the tagged specs that need real-backend wiring before they can go green.
