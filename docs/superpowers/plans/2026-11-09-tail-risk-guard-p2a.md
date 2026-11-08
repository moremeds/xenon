# Tail-risk guard P2 — atomic close, IB + Futu (plan v4, 2026-11-09)

Spec: `docs/superpowers/specs/2026-11-08-tail-risk-guard-design.md` §5.4, §6 (P2), §9.
Inputs: Fable advice (`tmp/p2-fable-advice-2026-11-09.md`); Astra plan review v1 (`tmp/astra-review-p2a-plan/astra.txt`,
CHANGES NEEDED: 4 blockers, 6 majors — all accepted, mapped below as [A1]…[A10]); Astra plan review v2
(`tmp/astra-review-p2a-plan-v2/astra.txt`: A3 A4 A5 A9 resolved; A1 A2 A6 A7 A8 A10 partial; new N1 N2) and v3 (`tmp/astra-review-p2a-plan-v3/astra.txt`: A7 A8
N1 N2 resolved; A1 A2 A6 A10 partial; new N3 — v4 below).
User 2026-11-09: P2 covers IB **and** Futu; a multi-leg option position closes as **one order**, never leg by leg;
VRP short put spreads have no TP. No live IB order by the lead; Futu tests on SIMULATE only.

## Delivery order (one PR each; each PR reviewed by Astra before merge)

| PR   | Content                                                                                                 | Live effect                                        |
| ---- | ------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| P2.0 | Guard incident lifecycle persisted; acks survive a restart [A10] (branch `feat/risk-guard-ack-persist`) | alerts only                                        |
| P2.1 | Exit contracts + `risk_exits` claim table + IB manual close (one structure)                             | IB orders, behind `XENON_RISK_CLOSE` (off)         |
| P2.2 | futu-api 10.03 → 10.11.7108 (= mini OpenD), alone, full Futu suite + SIMULATE smoke                     | none                                               |
| P2.3 | Futu strategy-view mapping + combo observation / mirroring / recovery [A4][A5]                          | none (read side)                                   |
| P2.4 | Futu combo close (capability, Gate 4, exit service) [A6][A8]                                            | Futu orders, behind `XENON_FUTU_COMBO_CLOSE` (off) |
| P2.5 | "Close all short premium" + UI polish; stop levels alert-only (`protection_plan`)                       | as P2.1/P2.4                                       |

Auto-exit (`armed`), PriceCondition / STP / OCA and the API probe stay P3 (order-contract change, spec §9.6; P0 TWS
probe not done).

## Close contract (P2.1, shared by IB and Futu)

**Canonical holding.** An exposure = the legs of one portfolio structure as signed holdings
`(instrument_id, signed_qty)`; `instrument_id` = IB `conId` / Futu code. Ratios = signed_qty / gcd; duplicate legs merged.
`exposure_key` = sorted `instrument_id:signed_ratio` joined by `|` (direction is in the sign) [A9].

**Close legs** = the negated holdings. Every effective close leg must reduce a current holding: `close_qty × |ratio|`
≤ the fresh holding of that leg, and the sign is opposite [A1]. Quantity = the largest whole number of combo units
the fresh holdings allow, or the operator's smaller number.

**Fresh evidence before claim** [A1][A2][A3]:

1. Scope: the request scope must equal the plan/position scope **and** the execution scope (IB: the verified
   `app.state` account, the account the order owner sends to; Futu: the configured account of the explicit env).
   Mismatch → 409 `RISK_SCOPE_MISMATCH`, nothing sent. Never substitute an account.
2. Holdings: a fresh positions read (IB: a single-flight sync pass, snapshot ≤ 60 s old after it; Futu: a fresh
   per-leg `position_list_query`). Stale or failed → 503 `RISK_EVIDENCE_STALE`.
3. Peer orders: a fresh scoped open-order read (IB `reqAllOpenOrders`, all clients; Futu order list). **Any** working
   order (any client, wizard TP, manual close, TWS) on any instrument of the exposure → 409 `RISK_PEER_ORDER`
   with the order ids; nothing sent. P2 never cancels a peer (operator cancels; P3 may).

**One serialization point with every Xenon order** [A1]: the claim is taken inside Gate 4's scope advisory lock
(`gate4.scope_lock_key`, the lock every `reserve_with_coverage` already holds), in the same transaction that
re-checks Xenon's own non-terminal `order_submissions` (PENDING, WORKING, PARTIALLY_FILLED, UNKNOWN,
MODIFY_PENDING — pending sends included) for any shared instrument (`conId` / Futu code); a hit → 409
`RISK_PEER_ORDER`, claim not taken. Conversely `evaluate_and_reserve` refuses any new Xenon order (wizard, manual,
protect, replace successor) with a leg on an instrument held by an active risk-exit claim → 409
`RISK_EXIT_IN_PROGRESS`. Claims are per instrument (one row per leg in `risk_exit_legs`, partial unique index on
(scope, instrument_id) over active claims), so overlapping exposure keys collide too. The exit's own place reuses
the claim (`client_attempt_id` match). **After the claim, before the send** the service re-reads holdings and
executions (fresh, as in step 2) and re-derives the close quantity from that residual; a peer order that filled
and went terminal between the first read and the claim therefore shrinks or cancels the exit (quantity 0 → claim
released as proven-unsent, 409 `RISK_NOTHING_TO_CLOSE`). Test: an ordinary close fills between the first holdings
read and the claim. Residual, documented in the runbook: a TWS / mobile order placed between the
fresh open-order read and the send is not seen (IB offers no cross-client lock); the next holdings read and the
guard's alerts show the result.

**Claim and attempt** (`xenon.risk_exits` + `xenon.risk_exit_legs`) [A2][A9]: one row per attempt: scope columns, `exposure_key`,
`client_attempt_id`, immutable `intent` jsonb (legs, ratios, qty, limit, cap, quote snapshot, evidence times),
`state` (`CLAIMED` → `SENT` → `WORKING` → `FILLED` | `PARTIAL_DONE` | `REFUSED` | `UNKNOWN`), timestamps.
Partial unique index `(broker, account_env, broker_account, exposure_key) WHERE state IN ('CLAIMED','SENT','WORKING','UNKNOWN')`.
The row (claim + intent + cid) commits **before** `order_entry.place` is called.

**Proven-unsent release** [N3]. A claim is released at once, with no broker reconciliation, when the attempt
provably never reached a broker: refused before `place` (scope, evidence, peer, quote, cap, quantity 0); refused
inside `place` before reservation or invocation (Gate 4 refusal with or without a submission row, quote-gate
`REJECTED`, preflight refusal, `ensure_order_allowed`); or abandoned while queued (`order_owner` settles it on
the request side as FAILED, zero sends). The service records the refusal reason on the attempt row. Tests: each
refusal path, then a retry that succeeds.

**Release = invocation fence + broker evidence** [A2]. For every attempt that may have been sent, a claim is released (attempt terminal) only when both hold:
(a) the attempt's order-owner invocation has finished: a started job settled on the worker (a broker-evidenced
terminal state set after the job ended), never the request side answering a deadline (502
`PLACE_OUTCOME_UNKNOWN` keeps the row UNKNOWN and the claim held); Futu: the worker job finished and the row is
terminal by a fresh read;
(b) broker evidence: the order is absent from fresh open orders, and executions for it were reconciled
(`order_fills` by permId / Futu deal list) after the job ended. Then the holdings are re-read and the residual
(possibly partly filled; `CANCELLED` counts as possibly partly filled) is what a new attempt may close.
`UNKNOWN` never auto-releases (runbook: operator resolves after checking TWS / Futu). A `FILLED` attempt does not
mark the exposure closed: the next fresh holdings read decides.

**Recovery on boot**: each non-terminal attempt resolves from `order_submissions` by `client_attempt_id` in scope
plus fresh open orders; else stays `UNKNOWN` + alert; never resent.

**Direction and price** [A7]:

- IB body: `BUY` envelope (CLAUDE.md combo rule), each leg's action = its close action (`BUY` closes a short,
  `SELL` closes a long), `nonGuaranteed: 0`, LMT, DAY. The snapshot legs are converted once from holdings; the body
  builder never re-reverses (test: credit put spread, debit call spread, iron condor, ratio 1×2, reversed holdings).
  Assertion before send: effective per-leg delta reduces each holding.
- Combo limit (IB BAG): signed combo price per CLAUDE.md (debit positive, credit negative) from
  `ComboQuote.signed_mid` (fresh: quote ≤ 10 s, both sides present; else 503 `QUOTE_UNAVAILABLE`). Marketable side
  for a BUY envelope = higher price. Cap in quote points per combo unit = min(0.10 × strike width, 0.50) for
  verticals; other structures min(0.10 × |mid|, 0.50), floor one tick. Limit = signed_mid + cap, rounded **toward
  marketable** to the tick (index options 0.05, else 0.01; IB code 110 → `LIMIT_OFF_TICK` surfaced). Combos skip
  today's single-leg quote gate (`order_entry.py:1016`), so this server-side check is the quote gate for closes.
- Single-leg close [N1]: an ordinary single order with a positive limit, never a signed price. BUY to close a
  short: limit = mid + cap; SELL to close a long: limit = mid − cap (≥ one tick, else refused); cap =
  min(0.10 × mid, 0.50), rounded toward marketable. It passes the existing single-leg quote gate
  (`quote_guard`) unchanged. Tests: long call / long put / long stock closes (SELL), short put / short call closes
  (BUY).
- No automatic reprice. Unfilled after 60 s → alert; the operator may cancel (existing cancel) and retry under the
  rules above.

**Gate 4** [A6]: a close goes through `order_entry.place` unchanged (Gate 4 + reservation). Structures that Gate 4
refuses with netting off (e.g. a covered call spread close → `COMBO_SPLIT_REQUIRED`) are refused as-is with the
reason; **never** legged. Tests per structure, netting on and off, puts and calls, cover consumed by other orders.

**Guard rails**: `ensure_order_allowed` inside the service (in-process bypass, incident rows #24/#41);
`XENON_READ_ONLY` / `orders_paused` → refused; new package `services/risk_exit/` (P1 `risk_guard/` stays
order-free); `XENON_RISK_CLOSE` env + runtime setting, default off.

## Futu specifics (P2.2–P2.4)

- SDK method is `position_list_query(..., show_option_strategy_view=False)` (futu-api 10.11.7108,
  `trade/open_trade_context.py:416`); `place_combo_order(combo_leg_list, price, qty, ...)` at `:1043`;
  `ComboLeg` at `common/constant.py:385` (code, trd_side, qty_ratio, position_id). Docs:
  https://openapi.futunn.com/futu-api-doc/trade/place-combo-order.html (shares the place_order limit 15 / 30 s).
- **Price** [A7]: `place_combo_order` takes per-leg `trd_side` and one `price`; the docs do not state its sign
  convention for a net credit vs a net debit or how the combo-level `trd_side` is derived. The SIMULATE probe of
  P2.3 must establish it (debit close and credit close of a vertical, unfillable limits, read back
  `strategy_type`, `trd_side`, `price`). Until that is recorded in `docs/architecture/futu-orders.md`, the Futu
  combo body builder refuses (`FUTU_COMBO_PRICE_UNVERIFIED`). The translation is then one function with tests
  per structure and sign.
- **Mapping** [A5]: a separate fresh strategy-view read; a structure maps to one strategy position only when every
  leg's code, side, quantity and strategy membership match exactly; ambiguous or stale → refused. The per-leg
  holdings feed used by Gate 4 stays as is. Selected `position_id`s go into the attempt intent.
- **Observation first** [A4]: the observer learns combo orders (`strategy_type`, `combo_legs`): immutable leg
  identity, combo-unit vs leg-unit quantity (no envelope/leg double count), exact-remark recovery after a lost
  reply, fill mirroring into `order_submissions` / `order_fills`. Tests: accepted-without-read, lost reply, partial
  fill, cancel, duplicate observation, unknown status, restart.
- **Gate 4 legs** [A6]: each Futu combo leg is stored as a durable `coverage.Leg` with qualified metadata from the
  IB contract of the same OCC option (the resolver single-leg Futu places already use), `legs.futu_code` per leg,
  and the mapped `position_id`. `guaranteed` stays False for Futu combos unless Futu documents atomic (all-or-none)
  execution for the admitted structures; SIMULATE fills alone never promote it (they cannot prove partial-leg fills
  impossible). With `guaranteed` False netting never applies, so a Futu call-spread close that needs netting is
  refused, not legged. Put-spread closes (VRP) need no netting (puts consume no cover).
- **Admission** [A8]: `broker_capabilities` FUTU gets a `combo_close` capability; the FUTU dispatch in
  `order_entry.place` routes `type: combo` only through a new explicit `combo_close` check (today's `"combo"`
  refusal at `order_entry.py:2327` stays for every other combo body). On only with `XENON_FUTU_COMBO_CLOSE` and
  the env in `XENON_FUTU_TRADING`. Accepted terms: US option legs only (`OPT`, same underlying, 2–4 legs), LMT,
  DAY, regular session; every leg reduces a holding with a mapped `position_id` (close-only). Opening combos,
  stock legs, modify and replace stay refused. Adapter in `execution/futu_orders.py`;
  `order_path_caller_allowlist.py` adds `place_combo_order` to the Futu mutation list.
- **Acceptance** [N2]: the SIMULATE strategy position is prepared outside Xenon (by the user in the Futu / moomoo
  app on the paper account; Xenon gains no opening-combo path for a fixture). Xenon then closes it through the API
  end to end. If SIMULATE cannot hold a strategy position with close ids, Futu combo close stays off and is
  reported unverified; no live substitute.

## Guard incident lifecycle (P2.0) [A10]

Implemented on `feat/risk-guard-ack-persist` (no schema change). One serialized writer appends the whole set of
**applied** acks as an `ack_state` row in `risk_events` (incident id, sent and highest severity, latest worsening
reference) on every change of that set and once at process start; a failed write retries every 5 s and shutdown
tries once more. An ack row written but not applied (`changed`) never enters the state. Before the first
evaluation a new process restores the newest `ack_state` row of the current ET date; every field is
type-checked, an invalid entry (or a market entry without its reference) is dropped and logged, so that incident
pages again; a failed read restores nothing (fail loud). Residual: a state change whose writes all failed before
the process died is lost (the older state is restored; a higher tier still alerts at once). Tests: restart while
acked, worsening during downtime, worsening already sent, unapplied ack, invalid entries, failed read, retry and
shutdown flush, id uniqueness at the same wall time.

## Tests and acceptance (per PR)

- Unit: exposure key canonicalisation; close-leg derivation; body builders (IB envelope/actions, Futu ComboLeg);
  signed limit/cap/tick; claim CAS (two closes of an existing exposure → one 409); attempt state table; recovery.
- Route: `POST /risk/close` — 403 READ_ONLY / ORDERS_PAUSED, 409 `RISK_EXIT_IN_PROGRESS` / `RISK_PEER_ORDER` /
  `RISK_SCOPE_MISMATCH`, 503 `RISK_EVIDENCE_STALE` / `QUOTE_UNAVAILABLE`, Gate 4 refusal passthrough.
- Browser (Playwright, stubbed API): CLOSE → confirm shows legs, signed limit, cap, qty, account → payload;
  pending / UNKNOWN rendering; close-all stops at the first failure.
- Live: the first IB close is the user's, on a position they choose, after reading the confirm screen.

## Approvals needed from the user

1. New tables `xenon.risk_exits` + `xenon.risk_exit_legs` (P2.1) and `xenon.protection_plan` (P2.5); Gate 4
   `evaluate_and_reserve` gains the risk-exit claim check (P2.1).
2. futu-api SDK upgrade (P2.2).
3. Enabling `XENON_RISK_CLOSE` / `XENON_FUTU_COMBO_CLOSE` in prod (after each PR's acceptance).
