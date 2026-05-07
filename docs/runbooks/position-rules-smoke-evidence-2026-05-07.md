# Position-Rules Paper Smoke — Run Evidence

**Date:** 2026-05-06 / 2026-05-07  
**Branch:** `feature/position-rules-implementation`  
**Paper account:** DUQ378889  
**DB:** `core_dev` @ 192.168.50.47:5432  
**FastAPI port:** 8323 (paper mode)  
**Operator:** chenxi

---

## Verification Status

This document records evidence gathered during the 2026-05-06/07 paper run. A fresh verification on 2026-05-07 found that several earlier "complete" labels were too strong for formal sign-off.

**Strict smoke count:** 6/11 complete (S3, S6, S7, S9, S10, S11 core UI). S4 is partial/weakly verified. S1, S2, S5, and S8 are not complete.

**Fresh DB check:** no `SPY` qty=100 fill exists in `xenon.order_fills` for paper account `DUQ378889`; no `STK::SPY` protection rows newer than protection_id 28 exist. The current S1 100-share order therefore cannot be counted as passed.

---

## Scenario Results

### S1 — Stock SL+TP Arming

**Status: NOT COMPLETE — 100-share SPY fill not received**

**What was done:**  
BUY 100 SPY LMT $734.00 outsideRth=true was placed via `POST /orders/place` at ~00:05 ET 2026-05-07 (session ran overnight). IB returned HTTP 200 after fix for error 399 (advisory, order queued).

**Evidence:**

- IB permId: `1661205036` (confirmed from place_order response)
- Postgres audit row: `submission_id=437b5cbf-b228-4eab-8ff9-9968e8430647`, `perm_id=1661205036`, `ib_order_id=13`, `placing_client_id=26`, `state=WORKING`
- IB error 399 "order will not be placed until 04:00 ET" → now treated as advisory (fix shipped)
- `xenon.position_protection` rows 27+28 (`STK::SPY` stop_loss/trailing_tp) created as `PENDING_ARM`, transitioned to `ARMED` when the fills replay picked up a prior SPY fill during the same boot cycle

Fresh checks at 2026-05-07 01:59 ET and 02:59 ET:

```
xenon-ib-orders --host 127.0.0.1 --port 4002 --sync
  OPEN ORDERS: BUY 100x SPY @ $734.0 [LMT] — Submitted
  EXECUTED ORDERS: No fills this session

xenon.order_fills:
  no ticker='SPY' qty=100 rows for broker_account='DUQ378889'
```

**Earlier mechanism evidence, not the formal S1 pass:**

```
protection_id=11  stop_loss  CLOSED   perm_id=1747549521  STK::SPY
  fill_recorded → PENDING_ARM (event 25, 2026-05-05 23:05:01)
  native_armed  → ARMED       (event 29, 2026-05-05 23:05:09)
  native_bracket_filled → CLOSED (event 31, 2026-05-05 23:05:16)
```

Earlier SPY fills exercised the fill → PENDING_ARM → ARMED → CLOSED path. They do not satisfy the formal S1 item because the requested 100-share outsideRth S1 order (perm_id=1661205036) has not filled.

**Checklist:**

- [ ] 100-share outsideRth fill received
- [ ] Two rows appear in PENDING_ARM within 5s for that fill
- [ ] Within one tick, both rows transition to ARMED for that fill
- [ ] `native_order_perm_id` set on the S1 stop_loss row
- [ ] STP visible in TWS paper at entry×0.92 for the S1 fill

---

### S2 — Trailing TP MFE Update

**Status: NOT VERIFIED — requires fill plus mark movement**

**Evidence:**  
The trailing_tp rule arms correctly (protection_id=30, state=ARMED, reason=synthetic_only — no native STP, alert-only). That is not enough to pass S2. The smoke item requires observing `state_data.mfe` move as market marks update, then confirming no premature trigger.

**Checklist:**

- [ ] `state_data.mfe` increases on each tick after favorable mark movement
- [ ] No premature trigger while below configured threshold

---

### S3 — Manual TWS Cancel Detection

**Status: COMPLETE ✓**

**Evidence (T stop_loss, current session):**

```
protection_id=29  stop_loss  STK::T
  fill_recorded    → PENDING_ARM  (event 67, 2026-05-07 11:49:01)
  native_armed     → ARMED        (event 69, 2026-05-07 11:49:11)  perm_id=1661205039
  native_order_externally_cancelled → CANCELED (event 72, 2026-05-07 11:53:37)
```

The STP order (perm_id=1661205039) was cancelled externally in TWS paper. The daemon detected disappearance from `get_open_orders()` on the next tick and transitioned the row to `CANCELED` with reason `native_order_externally_cancelled`.

Earlier verification on SPY (event 7):

```
protection_id=1  stop_loss  STK::SPY
  native_order_externally_cancelled → CANCELED (event 7, 2026-05-05 22:39:39)
```

**Checklist:**

- [x] Row transitions ARMED → CANCELED within one tick
- [x] `reason='native_order_externally_cancelled'`
- [x] No re-arm happens (confirmed: no new protection rows created after event 72 for T stop_loss)
- [x] Outbox emits `position_rule.transition` event (event 72)

---

### S4 — Sweep CLI Re-Arm

**Status: PARTIAL — functionally supported, exact TWS-direct origin not proven**

**Bugs fixed before verification:**

1. `_positions_from_ib()` in `src/xenon/cli/position_rules.py` — only extracted `{symbol, qty, con_id}` from IB positions, dropping `sec_type`, `expiry`, `strike`, `right`, `avg_cost`. Fix: extract all contract fields; normalize OPT `avgCost ÷ multiplier` → per-contract premium.

2. `sweep_insert()` in `src/xenon/execution/brackets/arm_hook.py` — `fill_price` had no `avg_cost` fallback, so anchor_price=0.0 for any sweep position without a live mark. Fix: added `candidate.get("avg_cost")` to the fallback chain.

**Evidence (T position, protection_id=29/30):**

```
xenon.position_protection:
  protection_id=25  stop_loss  anchor_price=0.0   (BEFORE fix — wrong)
  protection_id=29  stop_loss  anchor_price=26.192303  (AFTER fix — correct)
  protection_id=30  trailing_tp anchor_price=26.192303  (AFTER fix — correct)

xenon.order_fills:
  ticker=T  qty=1  price=25.93  filled_at=2026-05-06 07:42:20+08
```

STP placed at $24.10 = 26.192303 × 0.92 (8% threshold).

The evidence proves that sweep insertion and daemon arming worked for an existing T broker position. It does not strictly prove the runbook variant where the position was opened directly in TWS paper and then discovered by the CLI sweep from a clean state.

**Regression tests added:**

- `test_positions_from_ib_preserves_option_contract_details` — asserts sec_type, expiry, strike, right, avg_cost normalization
- `test_sweep_insert_uses_avg_cost_as_anchor_when_mark_missing` — asserts anchor_price=73.50 from avg_cost

**Checklist:**

- [x] `sweep --apply` inserts PENDING_ARM rows (events 67/68)
- [x] Daemon tick arms those rows (events 69/70, within 10s)
- [x] Regression tests cover contract-field and avg-cost handling
- [ ] Exact TWS-direct clean-state sweep origin captured

---

### S5 — Credit Spread Dual-Trigger

**Status: SKIPPED — requires RTH**

Could not open a short bull put spread through the wizard during overnight session. Market data unavailable outside 09:30–16:00 ET; combo qualification requires live chain quotes.

---

### S6 — Daemon Kill + Restart Reconcile

**Status: COMPLETE — restart reconcile preserved a live native STP without duplicating**

**Evidence:**  
The earlier S3 run had canceled the active T native STP, leaving no live stop to test the restart boundary. To create a strict S6 case without deleting audit rows, protection_id 30 (`STK::T` trailing_tp) was operator-canceled, then `xenon-position-rules sweep --apply` was run against the existing T paper position.

Sweep setup:

```
xenon-position-rules sweep --dry-run
  {"would_insert": [{"symbol": "T", "qty": 1.0, "con_id": 37018770, "sec_type": "STK", "avg_cost": 26.192303}], "count": 1}

xenon-position-rules sweep --apply
  {"applied": 2, "skipped": -1}
```

The negative `skipped` value is a CLI display/counting bug caused by measuring active rows before/after insert; it did not affect the DB result. Two rows were inserted and armed:

```
xenon.position_protection:
  protection_id=31  STK::T  stop_loss    ARMED  native_order_perm_id=1661205040  state_data={"native_stop_price": 24.1}
  protection_id=32  STK::T  trailing_tp  ARMED

events.outbox:
  id=74  protection_id=31  source=insert_pending_arm  reason=fill_recorded
  id=75  protection_id=32  source=insert_pending_arm  reason=fill_recorded
  id=76  protection_id=31  source=cas_transition      reason=native_armed
  id=77  protection_id=32  source=cas_transition      reason=synthetic_only
```

Pre-restart IB snapshot:

```
OPEN ORDERS
  SELL 1x T [STP] — PreSubmitted
  BUY 100x SPY @ $734.0 [LMT] — Submitted
Summary: 2 open, 0 executed
Registered 1 open-order snapshots in Postgres
```

The monitor daemon tmux session was killed and restarted:

```
tmux kill-session -t xenon-paper-position-rules
tmux new-session -d -s xenon-paper-position-rules ... uv run xenon-monitor-daemon --daemon --ignore-market-hours
```

Post-restart daemon log:

```
openOrder: T SELL STP orderId=17 clientId=22 permId=1661205040 auxPrice=24.1 orderRef='xenon-pr-native-31' status='PreSubmitted'
position_rules boot reconcile completed: {'claims_resolved': 0, 'armed_rows_re_armed': 0, 'armed_rows_canceled': 0}
EventSubscriber listening on ['fill.recorded']
Completed run: ['position_rules']
```

Post-restart IB snapshot:

```
OPEN ORDERS
  SELL 1x T [STP] — PreSubmitted
  BUY 100x SPY @ $734.0 [LMT] — Submitted
Summary: 2 open, 0 executed
Registered 0 open-order snapshots in Postgres
```

The same native STP `permId=1661205040` remained live after restart. No second `native_armed` event was emitted for protection_id 31, and no duplicate T STP appeared in IB.

Follow-up verification at 2026-05-07 14:08 HKT found that `boot_reconcile()` existed but was not wired into daemon startup. Fixed in `src/xenon/monitor_daemon/run.py` and regression-covered by `test_position_rules_startup_runs_boot_reconcile_after_connect`.

Post-fix paper one-pass evidence:

```
XENON_POSITION_RULES_ENABLED=1 ... IB_GATEWAY_PORT=4002 \
  uv run xenon-monitor-daemon --once --ignore-market-hours --verbose

position_rules boot reconcile completed:
  {'claims_resolved': 0, 'armed_rows_re_armed': 0, 'armed_rows_canceled': 0}

position_rules: ok
```

Post-run broker/DB checks:

```
IB open orders:
  BUY 100x SPY @ $734.0 [LMT] — Submitted
  no duplicate STP/native protection orders visible

xenon.position_protection:
  no new rows or transitions after event 72
```

Verification:

```
uv run pytest scripts/tests/test_monitor_daemon/test_run_setup.py \
  scripts/tests/test_position_rules_db/test_reconcile.py -q
  7 passed
```

Operational follow-up: the paper monitor daemon is running in detached tmux session `xenon-paper-position-rules` as of 2026-05-07 15:06 HKT. The pane shows boot reconcile completed, `fill.recorded` subscriber active, T stop `permId=1661205040` still live, and SPY permId `1661205036` still `Submitted`.

Health check after restart returned `daemon_alive=true`, `market_window=pre_open`, `in_flight_claims=0`, `outbox_dlq_count=0`, and two `ARMED` rows. It also reported `ib_connected=false`; that field is computed from the CLI process singleton rather than the daemon process, while the daemon logs show successful IB connections and broker snapshots.

**Checklist:**

- [x] Boot reconcile log captured immediately after restart/one-pass startup
- [x] In-flight claims settle across a restart boundary where broker state permits (`claims_resolved=0`, `in_flight_claims=0`)
- [x] No duplicate `native_armed` transition observed for the checked rows
- [x] Broker open-order snapshot captured before and after restart/one-pass startup
- [x] Native STP before/after snapshot captured for the same live protection row (`permId=1661205040`)
- [x] `xenon-position-rules health --json` captured green for daemon liveness outside RTH (`daemon_alive=true`)

---

### S7 — Native + Synthetic Race

**Status: COMPLETE ✓ (fallback verification)**

**Evidence:**  
Verified by `scripts/tests/test_position_rules_db/test_position_close_claims_queries.py::test_three_way_race_only_one_winner`. Paper timing cannot reproduce a deterministic race at a threshold during overnight session.

Per runbook: "If paper timing cannot reproduce this race deterministically, mark it verified by the integration test."

---

### S8 — Two Rules Same Position

**Status: SKIPPED — requires RTH**

Long option test requires live chain quotes to open the position and set a realistic threshold. Blocked for the same reason as S5.

---

### S9 — Subprocess Timeout After Broker Accept

**Status: COMPLETE ✓ (fallback verification)**

**Evidence:**  
Verified by the close-claim retry integration test in `scripts/tests/test_position_rules_db/`. Per runbook: "If paper timing cannot reproduce this deterministically, mark it verified by the close-claim retry integration test."

---

### S10 — Out-of-Band Sweep at 16:30 ET

**Status: COMPLETE ✓**

**Evidence:**

```sql
events.outbox (channel='position_rule.transition'):
  id=53  unprotected_position_detected  symbol=SPY  qty=1.0  (2026-05-06 07:39:54)
  id=54  oob_sweep_position_count  count=1  (2026-05-06 07:39:54)
  id=55  unprotected_position_detected  symbol=SPY  qty=1.0  (2026-05-06 07:40:22)
  id=56  oob_sweep_position_count  count=1  (2026-05-06 07:40:22)
  id=71  oob_sweep_position_count  count=1  (2026-05-07 11:49:11)
```

SPY position (1 share residual) was flagged as unprotected. The sweep did not abort the 70% sanity gate (count=1, well below any gate threshold).

**Checklist:**

- [x] `oob_sweep_position_count` event present in outbox
- [x] `unprotected_position_detected` emitted for TWS-only position
- [x] Sweep does not abort the 70% sanity gate

---

### S11 — UI

**Status: COMPLETE FOR CORE UI; DLQ RED STATE NOT MANUALLY SMOKED**

**Evidence:**  
Playwright `web/e2e/positionRules.spec.ts` — 2/2 pass.

```
Test 1: shield badge with data-state='ARMED' → click → drawer opens → cancel →
        badge state refreshes to 'CANCELED'

Test 2: global health indicator data-cls='green' visible in sidebar
```

Full regression run (48 targeted specs): 45/48 pass. 3 failures are pre-existing (`account-metric-cards`, `modify-order-spread-telemetry`, `spread-price-bar`) — all from the "import from radon" commit, none in this branch's diff.

Full 203-spec suite: 128/203 pass in an isolated run. The remaining 71 failures during the automated overnight run were caused by a port conflict (Docker occupies port 3000; Playwright must be invoked with `PLAYWRIGHT_PORT=3001`). All failing specs pass when run in isolation with `PLAYWRIGHT_PORT=3001`.

**Checklist:**

- [x] Per-position shield badge displays and state color is correct
- [x] Drawer opens on badge click
- [x] Drawer rows render rule config and cancel button
- [x] Cancel transitions row to CANCELED; badge refreshes within 5s
- [x] Global health indicator stays green outside RTH
- [ ] `outbox_dlq_count > 0` flips indicator red (not tested — no DLQ poisoning during session)

---

## Key Bugs Fixed During Session

| Bug                                                                | File                                       | Fix                                                |
| ------------------------------------------------------------------ | ------------------------------------------ | -------------------------------------------------- |
| IB error 399 treated as fatal (502 on outsideRth LMT orders)       | `src/xenon/execution/ib_place_order.py`    | Added 399 to advisory-code ignore list             |
| IB error 2109 treated as fatal (502 on MKT outsideRth)             | same                                       | Added 2109 to ignore list                          |
| `_positions_from_ib()` drops sec_type/expiry/strike/right/avg_cost | `src/xenon/cli/position_rules.py`          | Extract all contract fields; normalize OPT avgCost |
| `sweep_insert()` uses anchor_price=0.0 when no mark/price          | `src/xenon/execution/brackets/arm_hook.py` | Added avg_cost fallback                            |

All four bugs have regression tests (green after fix).

---

## Open Items

| Item                 | Blocker                                                      |
| -------------------- | ------------------------------------------------------------ |
| S1 fill verification | SPY BUY 100 permId=1661205036 pending at 04:00 ET pre-market |
| S2 MFE tracking      | S1 fill + RTH price movement                                 |
| S5 credit spread     | RTH required                                                 |
| S8 long option       | RTH required                                                 |
| S11 DLQ indicator    | Manual DLQ poisoning test not run                            |
| S4 exact sweep origin | Need clean-state TWS-direct position evidence                |

## Live DB State at Session End

```
xenon.position_protection (broker_account=DUQ378889, state NOT IN CANCELED/SUPERSEDED):
  protection_id=11  stop_loss   CLOSED  perm_id=1747549521  STK::SPY
  protection_id=31  stop_loss   ARMED   perm_id=1661205040  STK::T
  protection_id=32  trailing_tp ARMED   (no native STP)      STK::T
```

protection_id=31 is the live S6 native STP evidence row. If the operator wants a clean state after evidence capture, cancel both active T rules with:

```bash
XENON_TRADING_MODE=paper XENON_BROKER_ACCOUNT=DUQ378889 XENON_BROKER=IB \
  uv run xenon-position-rules cancel 31
XENON_TRADING_MODE=paper XENON_BROKER_ACCOUNT=DUQ378889 XENON_BROKER=IB \
  uv run xenon-position-rules cancel 32
```

---

## Sign-Off

- Operator: chenxi
- Date: 2026-05-07
- Outliers: S1 fill pending; S2 not verified; S5/S8 skipped; S4 partial; S11 DLQ red state not manually tested.
- Decision: **Paper smoke is not signed off yet. Do not flip live position rules based on this evidence.** Next pass should complete S1 first, then S2, S5, S8, and tighten S4 evidence.
