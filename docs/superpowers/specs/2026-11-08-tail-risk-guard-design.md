# Tail-Risk Guard — design (v3: Fable advice, Astra review x2, user decisions 2026-11-08)

Status: P0 (read-only + manual live probe) and P1 approved; P2+ need later approval. Nothing built. Branch `feat/tail-risk-guard`.
Inputs: lab-vrp (signal-lab), argon-22, helium-vrp, Fable advisory, Astra
review (both local in `tmp/trg/`), code scan at `e673ad9d`.

## 1. Why

On 2026-11-05 SPX fell fast through the user's short put strikes and the
account took a large loss. The user asked for:

1. Automatic scan of **IB and Futu** positions; after an opening fill (user
   opens in RTH) attach a **stop-loss** and a **trailing take-profit**.
2. **Warning** when SPX falls too fast — email every 30 s while it holds.
3. **Close-out** for tail events (11-05 intraday style, COVID-2020 style).
4. Coverage of the coming ~23 h trading session.
5. A helium report that sets stop / take-profit levels per position from GEX,
   technicals and volume profile (§5.5).

## 2. Evidence

| Fact                                                                                                                                                                                                                                                              | Source                                           |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 11-05: no opening gap (open 7678.72 vs prior close 7688.62). 09:35 7645.8, 09:45 7592, 10:00 ~7563, 10:20 7497, morning low 7394.0 at 10:50; session low 7350.35 at 15:55; close 7353.73 (−4.36%).                                                                | lab-vrp, apex 5m bars                            |
| User short strikes crossed: 7580 ~09:45, 7490 ~10:25, 7460/7430 ~10:45.                                                                                                                                                                                           | lab-vrp                                          |
| Account 11-08: NLV $77.9k, ExcessLiquidity $6.0k (cushion 7.7%), ~+118 SPX delta, 4 short put spreads, width $113k = 1.45× NLV. 11-05 intraday cushion: not available.                                                                                            | lab-vrp                                          |
| COVID 2020: −33.9% in 23 sessions; open gaps up to −7.5%; up days to +9.4% inside the crash.                                                                                                                                                                      | lab-vrp, apex daily                              |
| **11-05 xenon SPX/VIX feed frozen all RTH** (argon `gex_snapshots` spot 7688.62, `spot_source=xenon_ws`; VIX 16.61 frozen, real high 28.36). HTTP 200 with stale data. Root cause (`docs/research/2026-11-08-risk-guard-p0.md`): IB sent no ticks; the relay published the prior close as `last` (`ib_tick_handler.js:58-105`). IB snapshots sparse since ~10-30; prod `/portfolio` 500 since the 11-08 boot (scope unresolved).                                  | argon-22, memory                                 |
| No validated intraday early-warning signal.                                                                                                                                                                                                                       | lab-vrp, argon-22                                |
| IB path: LMT only (`order_entry.py:895-915`); `outsideRth=False` (`ib_place_order.py:284-289`); combos default `NonGuaranteed=1` (`ib_place_order.py:297-307`); no bracket/OCA. ib_async has `StopOrder`, `PriceCondition`, `ocaGroup`, `parentId`.               | code                                             |
| `place()` resolves one app scope and overwrites body `account` (`order_entry.py:787, 1091`); core sets `order.account` (`ib_place_order.py:290`); foreign-client cancel/modify refused (`order_entry.py:1490`).                                                   | code                                             |
| Cancel can end UNCERTAIN (`order_entry.py:1530`); `permId=0` = unknown, not rejected (`:1190`); an accepted order can survive a failed persist (`:1268`).                                                                                                         | code                                             |
| `protect.py` places a guaranteed (`NonGuaranteed=0`, `:191`) GTC SELL BAG take-profit, but only for a wizard session (`:169, :300`). It is not a general protection service.                                                                                      | code                                             |
| Gate 4 counts **call** demand only; a short put consumes no cover (`coverage.py:298-303`). BAG call netting needs guaranteed + netting enabled (`:250-258`).                                                                                                      | code                                             |
| Futu live: stock STP/STP LMT/TRAIL/TRAIL LIMIT DAY/GTC RTH; options LMT/STP LMT/LIT; allowlist LMT-only by default; SIMULATE = LMT + stock MKT.                                                                                                                   | `futu-orders.md:26-61`, `futu_orders.py:102-119` |
| Python streaming quotes exist (`combo_quote_source.py:134`, `snapshot=False`).                                                                                                                                                                                    | code                                             |
| No email/push sender in xenon; ntfy used by Watchtower (`compose.prod.yml:184-190`); helium mails via Resend once per cron LLM run — unusable for 30 s.                                                                                                           | code, helium-vrp                                 |
| Guards: `orders_paused` blocks place/modify, allows cancel; `READ_ONLY` blocks all (`guards.py:132, 291`). Owner halt latch clears only on restart (`order_owner.py:165, 305`). Empty post-reconnect snapshots are distrusted (`ib_activity_mirror.py:263, 299`). | code                                             |
| Calendar has no early closes (`market_calendar.py:16`). Weekly IBC restart sat at 2FA (11-07).                                                                                                                                                                    | code, lab-vrp                                    |

## 3. Size

At 1.45× NLV of width and a 7.7% cushion, no exit logic prevents a large loss
on a −3% gap. The guard reduces intraday damage; size decides gap damage. Every
alert and a daily line print width/NLV and cushion. Changing size is the user's call.

## 4. Principles

- Measure, do not predict. Triggers: realised speed, level breaks, margin.
- Prefer resting broker-side orders (survive xenon/feed/gateway failure);
  synthetic exits only where the broker cannot hold the order.
- Stale is an alarm, never "no quote".
- **Orders never act on an unresolved predecessor.** Every exit is a durable
  state machine; an unknown outcome never produces a fresh attempt.
- Detect + notify ship alone first. Manual close before automation. Order
  capability is proven with live unfillable orders before use (user: IB paper is unreliable).
- All orders through the order owner / Futu adapter and every guard. One scope
  per plan (the configured `AccountScope`); no cross-account acting.
- Combo exits are **guaranteed** whole-BAG orders (`NonGuaranteed=0`).

## 5. Architecture

Module family `src/xenon/api/services/risk_guard/`, started in lifespan like
the wizard risk alert (`server.py:279-327`).

### 5.1 Market watcher (read-only)

- Sources: IB streaming `SPX` + `VIX` on the `data` role; Futu `SPY` (extended
  and overnight). Comparisons use **returns**, never raw levels (SPY ≠ SPX);
  VIX is a separate up-is-bad series.
- Ages from a monotonic receive clock; source timestamps outside ±5 s of
  receive time are rejected. An unchanged print is valid; "stale" = no tick
  received for 20 s in that instrument's session.
- Session per instrument (RTH / extended / overnight / closed) from
  `market_calendar` + an **early-close table** (new; calendar lacks it).
- Reconnect: re-subscribe on owner/data reconnect; until the first fresh
  tick the watcher is BLIND, not "fine".
- All SPX sources stale → `FEED_STALE`; fresh sources disagree on 5 m return
  by > 0.3 pp → `FEED_DIVERGENCE`. A price trigger needs two agreeing fresh
  sources, or one fresh source with BLIND already raised.
- Metrics: returns over 5/15/30/60 min, vs prior close, new session low, level
  breaks. Window rules:
  - Each window is anchored on receive time and sampled on a 1 s grid (last
    value carried forward). Returns use the sample at `now − W` within ±2 s.
    With no such sample, the window is `UNAVAILABLE`, not 0%.
  - A window fires only when it is full (warm-up). It resets at each session
    boundary (RTH open, extended, overnight) and after a reconnect gap > 20 s.
  - "Prior close" = the official prior RTH close. "Day" = since that close.
  - A source with no exchange timestamp uses the receive time and is marked
    `ts=receive`. It can trigger only together with a second source.

### 5.2 Account watcher (read-only)

Configured IB live scope: cushion, ExcessLiquidity, NLV, width/NLV, max loss/NLV,
SPX delta, spot distance to each short and long strike. Source
`account_snapshots.payload`; its age is shown, and cadence during an alert is a
P0 check. Futu account fields: v2.

**Account risk ratios** (user request 2026-11-08). All three are in P1, read-only.

| Ratio    | Definition                                                                                                                                                                                                                                                                          | Warn    | Severe  | Critical |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- | ------- | -------- |
| Leverage | Σ max loss / NLV. Max loss per credit vertical = (width − credit) × multiplier × qty; debit structures = debit paid; short stock and naked positions are excluded (Gate 4 blocks them).                                                                                             | > 0.50× | > 0.75× | > 1.00×  |
| Drawdown | NLV / peak NAV − 1. The peak is the max `nav_history.nav` (`source='close'`) for the scope, adjusted for net deposits and withdrawals from the Flex cash-flow data. Without cash-flow data the alert says "unadjusted". The window is all-time, plus a 30-day peak shown beside it. | ≤ −5%   | ≤ −10%  | ≤ −15%   |
| Cushion  | ExcessLiquidity / NLV (IB `Cushion`).                                                                                                                                                                                                                                               | < 20%   | < 12%   | < 8%     |

- Thresholds are defaults in runtime settings (tuning category), editable in `/admin`.
- **Cadence**:
  - Leverage and drawdown are standing conditions, not events. Each one sends one push + email when it **crosses** a level (in either direction), then again only when it worsens by one more step: +0.25× for leverage, −2 pp for drawdown. A daily 09:00 ET line reports all three ratios.
  - Cushion < 8% sits next to IB auto-liquidation, so it uses the 30 s cadence (CRASH tier). Cushion < 12% maps to FAST, and < 20% maps to WATCH.
- Example. On the 11-08 numbers (width 1.45× NLV, cushion 7.7%), leverage would sit at Critical and cushion at CRASH. The guard does not reduce size. It only makes the size visible.
- Later (P2): an order ticket warning when a new order would push leverage over the next level.

### 5.3 Tiers + notifier

Fire times are **computed by the §8 replay**, not asserted. Hypothesis only
(arithmetic on the 5m points in §2, not replayed): "spot within 0.5% of a short
strike" (7580 × 1.005 = 7617.9) first holds between 09:35 (7645.8) and 09:45
(7592). Historical cushion and positions for 11-05 are not available, so the
replay tests the price triggers only for that day.

| Tier  | Trigger (any)                                                                                                                               | Cadence                                                               |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| WATCH | 15 m ≤ −0.75% · day ≤ −1.0% · key level broken · cushion < 20%                                                                              | once: push + email                                                    |
| FAST  | 30 m ≤ −1.5% · day ≤ −2.0% · spot within 0.5% of a short strike · cushion < 12%                                                             | push + email every 30 s until ack; then on worsening / tier rise only |
| CRASH | day ≤ −3.0% · 60 m ≤ −2.5% · a long leg ITM · cushion < 8%                                                                                  | 30 s + close-out prompt                                               |
| BLIND | FEED_STALE · FEED_DIVERGENCE · owner halted · port 4001 closed (incl. 09:00–09:25 ET pre-open) · Futu OpenD down (Futu positions) · DB down · account scope unresolved (P0 §3) · IB snapshot age > 120 s in session · data role not connected · relay `stale:true` on a needed instrument | 30 s until ack; UI banner "stops offline"                             |

- Message: SPX + source ages, speeds, levels broken, cushion, width/NLV, per
  position strike distance, est. loss, protection state, one action line.
- Channels from xenon: **ntfy push** (primary) + **Resend email** (xenon token;
  plan quota and rate limit UNVERIFIED). Daily email cap → push only beyond it.
- **The notifier does not depend on the DB.** It sends first, then records
  `risk_events` best-effort; a DB outage is itself a BLIND alert.
- **Two independent channels of state.** _Market severity_ (WATCH < FAST <
  CRASH) and _health_ (each BLIND cause: feed stale, divergence, owner halted,
  gateway, OpenD, DB) are tracked separately. Each active condition is an
  **incident** keyed by its cause.
- **Ack is per incident.** It is stored in `risk_events` with the incident key,
  not as one global mute. An ack never suppresses:
  - a new incident (a different cause);
  - a severity rise on the market channel;
  - a "worsening" event. Worsening means a new session low that is ≥ 0.25%
    below the low at ack time, a cushion that is ≥ 2 pp lower, or one more
    short strike crossed.
    After an ack, the incident re-sends only on worsening. It closes when its
    condition stays clear for 5 min.
- If the DB is down, ack is unavailable and alerts continue (safe direction).
- Dead-man: heartbeat every 30 s with last receive time per source; external
  checker on a second host alerts on "missing" and "alive but blind".
- New table `xenon.risk_events` — **schema approval needed**.

### 5.4 Protection engine (P2+, orders)

**Exposure and scope.** A plan belongs to one `AccountScope` and one exposure
key = legs only (conIds + ratios, **no quantity**), so partial fills and
resizes keep the same key. Before any exit, the engine takes an atomic DB
claim on `(scope, exposure_key)`: a unique active-exit row, or a row lock. A
second exit (manual button, automatic stop, reconcile) for the same exposure
waits or is refused. Quantity is a field of the plan, not part of the key. Only
orders this owner placed are managed; foreign-client protective orders
(TWS/mobile) are reported, never touched.

**A. Attach on open.** An opening fill (activity poller / fills replay; Futu
order worker) creates a `protection_plan` with lineage to the fill.
**Schema approval needed.** Default exits per structure:

| Position                               | Stop                                                                                                                                                                                                      | Take-profit                                                                                         |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Credit vertical (SPX short put spread) | P0 probe decides: (a) resting guaranteed closing BAG LMT with `PriceCondition` on SPX index, linked to the TP by OCA if IB accepts it; else (b) synthetic on the same trigger. Mark only as confirmation. | **none** — VRP spreads are held to expiry (user 2026-11-08). Optional per position, off by default. |
| Long option / debit spread             | mark ≤ 50% of debit (synthetic)                                                                                                                                                                           | fixed GTC LMT; trailing P3                                                                          |
| Stock / ETF, Futu live                 | native STP LMT GTC RTH                                                                                                                                                                                    | native TRAIL LIMIT                                                                                  |
| Stock / ETF, IB                        | IB STP/TRAIL type (order-path change)                                                                                                                                                                     | P4                                                                                                  |

**B. Exit state machine** (durable, per plan):
`ARMED → TRIGGERED → CANCELLING_PEER → PEER_RESOLVED → INTENT_PERSISTED → SUBMITTED → WORKING → (PARTIAL)* → FILLED | CAPPED | UNKNOWN`

- Before cancelling the peer TP: admission check (`ensure_order_allowed`,
  owner not halted, not READ_ONLY, place allowed). If a replacement could be
  refused, do not cancel; alert instead.
- If placement becomes forbidden **after** the peer cancel (`orders_paused`
  set, owner halts, READ_ONLY), the plan goes to `DEGRADED_UNPROTECTED`:
  CRASH-level alert every 30 s ("position unprotected: <structure>"), UI
  banner, and no automatic retry. It leaves that state only when a manual close
  runs or an operator re-arms the plan after placement is allowed again.
- OCA (if the P0 probe shows IB accepts it): acceptance also requires a live
  unfillable-order test of a TP partial-fill rule → the stop quantity shrinks to the residual (IB
  OCA type 2/3 behaviour); otherwise OCA is not used and the state machine
  alone gives mutual exclusion.
- Submit only after the peer cancel is **confirmed** and fills are refreshed;
  quantity = remaining exposure (a TP partial fill shrinks it).
- Intent + idempotency key are persisted before send; recovery after restart
  goes by `order_submissions` / orderRef / broker evidence. `UNKNOWN` → alert
  and manual handling; never a new attempt.
- Reprice toward a **dollar slippage cap**; at the cap, cancel (confirmed) →
  `CAPPED`, keep alerting on residual risk.
- Partial fills keep the plan alive for the residual. Cooldown and no re-arm
  apply only after the exposure is zero.

**C. Reconcile (60 s + boot).** Missing plans, changed exposure (partial close,
roll) → resize/replace via the same state machine. **Orphan cancel only on a
complete, fresh, account-scoped observation with reconciled executions**;
missing data ≠ zero exposure.

**D. Close-out.** v1: manual "close this structure" and "close all short
premium" buttons on the same state machine; the CRASH alert links to them.
Later opt-in `armed`: auto when CRASH and cushion < hard floor. Overnight when
options cannot trade: alert + hedge proposal only.

### 5.5 helium level report (parallel, read-only)

helium-vrp analyses positions via its read-only xenon key and argon data (GEX,
technicals, volume profile) and emails a level report (first run 2026-11-08).
Later it may publish JSON levels; xenon shows them as **suggestions the user
approves**, never on the trigger path. Levels are descriptive: no measured edge
for GEX persistence or measured-move levels; argon walls have a side bug;
argon spot was frozen on 11-05.

## 6. Phases

| Phase | Content                                                                                                                                                                                                                                                                                                   | Orders            |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------- |
| P0    | Read-only: 11-05 frozen-feed root cause; IB live entitlement SPX/VIX; snapshot cadence; Resend quota, ntfy topic, dead-man host; SPX option GTH hours; early-close list. **Live unfillable-order probe** (manual in TWS; protocol below): guaranteed closing BAG LMT with `PriceCondition`, BAG STP, OCA. | live, unfillable  |
| P1    | Market + account watcher, tiers incl. BLIND, ntfy + Resend notifier, ack, dead-man, `risk_events`, replay harness. **No orders.**                                                                                                                                                                         | none              |
| P2    | Protection plans + exit state machine + reconcile; attach-on-open with stop per P0 (TP only for non-VRP structures); manual close buttons; API-path probe behind `XENON_RISK_PROBE=1`.                                                                                                                    | yes               |
| P3    | Synthetic stops/trailing TP live behind `risk_guard_armed`.                                                                                                                                                                                                                                               | yes               |
| P4    | Native stock stops (Futu STP LMT/TRAIL LIMIT; IB STP/TRAIL).                                                                                                                                                                                                                                              | order-path change |
| P5    | Full 23×5: ES cross-check, outsideRth, Futu account watcher, hedge proposals.                                                                                                                                                                                                                             | yes               |

Hypothesis to be checked by the replay: on 11-05, P1 would have raised FAST in
the first 15 min, and BLIND for all of RTH, because the feed was frozen.

**Live unfillable-order probe** (user decision 2026-11-08: IB paper is not
reliable; probe on live with prices that cannot fill, then cancel):

- Account: live IB …31 only. The order sets `order.account` explicitly
  (never `managedAccounts()[0]`). Runs in RTH.
- Orders are **closing orders only**, for a spread the account already holds,
  quantity 1. A fill could only reduce risk, and the prices make a fill
  impossible:
  - BAG BUY-to-close the held vertical. LMT $0.05, far below its value.
  - `PriceCondition` SPX index < 5000, far below spot.
  - Guaranteed (`NonGuaranteed=0`). GTC.
- Cases to probe, one at a time:
  1. BAG LMT + `PriceCondition`.
  2. BAG STP / STP LMT with an aux price far away.
  3. Two orders in one `ocaGroup` (types 1 and 3).
     For each case, record: IB accept/reject, the error code, the status shown
     in TWS, and whether the order survives a gateway restart (GTC).
- Path: there is no xenon path for conditions, STP or OCA today. A one-off
  script with its own order connection would break the order-path policy
  (`order_path_caller_allowlist.py`). So:
  - **P0** = the user enters these orders by hand in TWS from a checklist
    (no code). This answers "does IB accept it".
  - **P2** = the same cases run again through the real order owner, behind
    `XENON_RISK_PROBE=1` (live, unfillable prices), before the feature uses them.
- Each probe order is cancelled at the end, and the cancel is confirmed. Evidence:
  the open orders before and after, plus the order ids, saved to
  `docs/research/2026-11-xx-risk-guard-live-probe.md`.

## 7. Out of scope

Predictive signals; automatic re-entry; automatic hedging; changing size.

## 8. Acceptance

- Replay harness on timestamped 11-05, 2020-02-24…03-23 and 2025-04 bars:
  report first crossings per tier (not tuned to §5.3); count WATCH/FAST fires
  over ≥ 12 months of 1 m data before thresholds are fixed.
- Failure injection: frozen source, divergent source, data reconnect, DB down,
  OpenD down, API kill (dead-man), clock skew, early close → expected tier.
- P2+: unit + route + browser regressions per CLAUDE.md order-path rules; TP
  partial fill during stop trigger; cancel UNCERTAIN; restart mid-exit;
  `orders_paused` mid-exit; whole guaranteed BAG; dollar cap; residual alerts.

## 9. Decisions and approvals (user, 2026-11-08)

1. **Approved, P1 scope:** new table `risk_events`; external services **Resend**
   (xenon-owned token) and **ntfy** topic; a second-host dead-man checker.
2. **Probe:** live with unfillable closing orders, not paper (see §6).
3. **VRP short put spreads are held to expiry: no take-profit.** Stop / exit
   only. TP (fixed, then trailing) applies to other structures (stocks, long
   options, debit spreads).
4. helium may copy the read-only `XENON_QUERY_API_KEY` into helium.env.
5. Not changed by the user, kept as in the plan: manual close buttons first,
   with `armed` later by opt-in; 30 s push + email until ack, then on
   worsening only.
6. Still to approve later: the `protection_plan` schema; the order-contract
   extensions (conditions/OCA/STP).

## 10. Open questions

1. Default stop levels per structure (helium level report feeds this).
2. Resend token and ntfy topic: the user provides them.
