# Position-Rules Paper-Account Smoke

Mandatory before flipping `XENON_POSITION_RULES_ENABLED=1` on the live account.

## Pre-Flight

- [ ] Phase 0 DST fix merged: `uv run pytest scripts/tests/test_monitor_daemon/test_market_hours_dst.py -xvs`.
- [ ] Current migrations applied to paper Postgres: `uv run alembic current`.
- [ ] `xenon.bracket_policies` has 8 seed rows.
- [ ] `wizard_protection` table is absent.
- [ ] `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py` is absent.
- [ ] `XENON_POSITION_RULES_ENABLED=1` is set in paper env and daemon is restarted.
- [ ] `xenon-position-rules health --json` returns `daemon_alive=true` and the expected `market_window`.

## Scenarios

### S1 - stock SL+TP arming

Open a 100-share long stock position in paper through the UI.

- [ ] Two rows appear in `xenon-position-rules list --state PENDING_ARM` within 5s: `stop_loss` and `trailing_tp`.
- [ ] Within one tick, both rows transition to `ARMED`.
- [ ] `native_order_perm_id` is set on the `stop_loss` row.
- [ ] In TWS paper, a working STP order is visible at exactly `entry * 0.92`.

### S2 - trailing TP MFE update

After S1, move the paper mark upward.

- [ ] `state_data.mfe` increases on each tick where mark exceeds prior MFE.
- [ ] No premature trigger occurs.

### S3 - manual TWS cancel detection

In TWS paper, manually cancel the stop-loss STP from S1.

- [ ] Within one tick, the row transitions `ARMED -> CANCELED` with `reason='native_order_externally_cancelled'`.
- [ ] No re-arm happens.
- [ ] Outbox emits a `position_rule.transition` event.

### S4 - sweep CLI re-arm

Open a position directly in TWS paper.

- [ ] `xenon-position-rules sweep` lists the symbol under `would_insert`.
- [ ] `xenon-position-rules sweep --apply` inserts `PENDING_ARM` rows.
- [ ] The next daemon tick arms those rows.

### S5 - credit spread dual-trigger

Open a short bull put spread through the wizard.

- [ ] `position_protection` shows `stop_loss` and `take_profit_fixed` rows.
- [ ] If the underlying breaches the short strike, `stop_loss` triggers.
- [ ] If debit-to-close drops to at most 50% of credit, `take_profit_fixed` triggers.

### S6 - daemon kill + restart reconcile

Force-kill the monitor daemon mid-tick, then restart it.

- [ ] Boot reconcile runs.
- [ ] In-flight claims settle to terminal state where broker state permits.
- [ ] No duplicate orders are submitted to IB.
- [ ] `xenon-position-rules health --json` returns green within one tick.

### S7 - native + synthetic race

Open a long stock with a native STP and price the underlying at the threshold.

- [ ] Exactly one MKT close reaches IB, verified through Flex Query.
- [ ] `position_close_claims` shows one winning claim.
- [ ] The losing rule path transitions to `SUPERSEDED`.

If paper timing cannot reproduce this race deterministically, mark it verified by `scripts/tests/test_position_rules_db/test_position_close_claims_queries.py::test_three_way_race_only_one_winner`.

### S8 - two rules same position

Open a long option and configure both `stop_loss` and `trailing_tp` so they can fire on the same tick.

- [ ] One MKT close reaches IB.
- [ ] One rule reaches `CLOSED`.
- [ ] The other reaches `SUPERSEDED`.

### S9 - subprocess timeout after broker accept

Disconnect IB Gateway immediately after broker accepts a MKT close but before subprocess return.

- [ ] Retry finds the existing order by `orderRef`.
- [ ] No re-submit occurs.
- [ ] Existing `perm_id` is attached to the claim.

If paper timing cannot reproduce this deterministically, mark it verified by the close-claim retry integration test.

### S10 - out-of-band sweep at 16:30 ET

After market close:

- [ ] `oob_sweep_position_count` event is present in outbox for today.
- [ ] Any TWS-only position emits `unprotected_position_detected`.
- [ ] The next sweep does not abort the 70% sanity gate.

### S11 - UI

- [ ] Per-position shield badge displays and state color is correct.
- [ ] Drawer opens on badge click.
- [ ] Drawer rows render rule config and cancel button.
- [ ] Cancel transitions row to `CANCELED`; badge refreshes within 5s.
- [ ] Global health indicator stays green outside RTH despite stale synthetic ticks.
- [ ] `outbox_dlq_count > 0` flips indicator red.

## Sign-Off

- Operator:
- Date completed:
- Outliers / unverified scenarios:
- Decision: proceed to live / block on follow-up
