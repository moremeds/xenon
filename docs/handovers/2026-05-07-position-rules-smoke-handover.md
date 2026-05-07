# Handover — Position-Rules Paper Smoke Completion

**Branch:** `feature/position-rules-implementation`  
**Worktree:** `/Users/chenxi/projects/xenon/.worktrees/position-rules-implementation`  
**Date:** 2026-05-07  
**For:** Codex (next session)

---

## What This Branch Does

Implements the position-rules engine: after every fill, `arm_hook` classifies the position and inserts `PENDING_ARM` rows into `xenon.position_protection`. The monitor daemon picks those up, places native IB stop-loss orders (STP), and monitors them for external cancels, fills, and OOB sweeps. A UI layer (shield badge, drawer, global health indicator) surfaces status in the portfolio view.

## Current State

**The branch is implementation-complete but paper smoke is not signed off.** All Python and TypeScript implementation, migrations, CLI, and tests are present. A paper smoke pass was attempted 2026-05-06/07 against account DUQ378889, but a fresh evidence audit found several scenario labels were overstated. Full evidence is in `docs/runbooks/position-rules-smoke-evidence-2026-05-07.md`.

**Smoke test scorecard:**

| S#  | Scenario                | Result                                                |
| --- | ----------------------- | ----------------------------------------------------- |
| S1  | Stock SL+TP arming      | **Complete** — USO qty=100 filled and armed           |
| S2  | Trailing TP MFE         | **Not verified** — needs fill plus mark movement      |
| S3  | Manual TWS cancel       | **Complete**                                          |
| S4  | Sweep CLI re-arm        | **Complete** — GM direct broker position swept/armed  |
| S5  | Credit spread           | **Partial** — filled/armed; triggers need marks       |
| S6  | Daemon kill+restart     | **Complete** — same live STP survived daemon restart  |
| S7  | Native+synthetic race   | **Complete** by allowed integration-test fallback     |
| S8  | Two rules same position | **Partial** — filled/armed; trigger race needs marks  |
| S9  | Subprocess timeout      | **Complete** by allowed integration-test fallback     |
| S10 | OOB sweep at close      | **Complete**                                          |
| S11 | UI                      | **Complete**                                          |

**Strict count:** 8/11 complete: S1, S3, S4, S6, S7, S9, S10, and S11. S5 and S8 are partial. S2 is not verified.

## Immediate Task: Continue With S2

S1 is now complete using a replacement `USO` paper fill. The stale SPY order was canceled through the auditable route, and the replacement fill created `STK::USO` protection rows:

```
Canceled stale SPY:
  POST /orders/cancel {"orderId":13,"permId":1661205036}
  order_submissions submission_id=437b5cbf-b228-4eab-8ff9-9968e8430647
  state=CANCELLED  reason_code=USER_CANCEL

Replacement S1:
  BUY 100 USO LMT 138.91 outsideRth=true
  response: orderId=19 permId=1661205043 initialStatus=Filled
  order_fills exec_id=00025b46.69fc0f4d.01.01
  fill price=132.54 filled_at=2026-05-07 16:19:12+08
  events.outbox ids=78-82
  protection_id=33 STK::USO stop_loss ARMED native_order_perm_id=1661205049 state_data={"native_stop_price":121.94}
  protection_id=34 STK::USO trailing_tp ARMED
  broker open order: SELL 100x USO [STP] — PreSubmitted
```

The FastAPI activity poller did not mirror the subprocess-client fill; `uv run xenon-ib-reconcile` inserted it into `xenon.order_fills` and emitted `fill.recorded`. Do not manually insert or delete audit rows.

Next strongest smoke target is S2: observe `protection_id=34` trailing TP `state_data.mfe` move on mark updates and confirm it does not trigger prematurely. Latest retry at 2026-05-08 01:25 HKT found the paper gateway and daemon up, but direct IB market-data probes still returned no usable marks. `USO`, `GM`, `SPY 750C`, `SPY 720P`, and `SPY 715P` returned `nan` bid/ask/last/close/marketPrice across market-data types 1, 2, 3, and 4; a `reqTickers` probe returned IB error 10197, `No market data during competing live session`. S2 remains blocked until the competing live session is cleared or the paper account gets an independent market-data stream.

Paper monitor daemon was restarted in detached tmux session `xenon-paper-position-rules` at 2026-05-07 14:12 HKT:

```bash
tmux capture-pane -pt xenon-paper-position-rules -S -80
tmux attach -t xenon-paper-position-rules
tmux kill-session -t xenon-paper-position-rules
```

Startup evidence in that pane shows `position_rules boot reconcile completed` and `EventSubscriber listening on ['fill.recorded']`.

```bash
PGPASSWORD=xenon_dev psql -h 127.0.0.1 -U xenon_app -d core_dev -c \
  "SELECT exec_id, qty, price, filled_at FROM xenon.order_fills
   WHERE ticker='SPY' AND broker_account='DUQ378889' AND qty=100
   ORDER BY filled_at DESC LIMIT 3"
```

Useful S1/S2 check queries:

```bash
PGPASSWORD=xenon_dev psql -h 127.0.0.1 -U xenon_app -d core_dev -c \
  "SELECT exec_id, submission_id, ticker, side, qty, price, perm_id, ib_order_id, filled_at
   FROM xenon.order_fills
   WHERE ticker='USO' AND broker_account='DUQ378889'
   ORDER BY filled_at DESC LIMIT 5"

PGPASSWORD=xenon_dev psql -h 127.0.0.1 -U xenon_app -d core_dev -c \
  "SELECT protection_id, rule_kind, state, native_order_perm_id, state_data, updated_at
   FROM xenon.position_protection
   WHERE broker_account='DUQ378889' AND position_key='STK::USO'
   ORDER BY protection_id DESC LIMIT 6"
```

If FastAPI is needed for order-route work, the dedicated S1 smoke instance was:

```bash
cd /Users/chenxi/projects/xenon/.worktrees/position-rules-implementation
DATABASE_URL=postgresql+psycopg://xenon_app:xenon_dev@127.0.0.1:5432/core_dev \
XENON_TRADING_MODE=paper \
XENON_BROKER_ACCOUNT=DUQ378889 \
XENON_BROKER=IB \
IB_GATEWAY_HOST=127.0.0.1 \
IB_GATEWAY_PORT=4002 \
XENON_POSITION_RULES_ENABLED=1 \
XENON_REGIME_GATE_DISABLED=1 \
uv run uvicorn xenon.api.server:app --host 127.0.0.1 --port 8323
```

## Known Infrastructure Gotcha: Playwright Port Conflict

Docker occupies port 3000. **Always run Playwright with `PLAYWRIGHT_PORT=3001`:**

```bash
cd web
PLAYWRIGHT_PORT=3001 npx playwright test e2e/positionRules.spec.ts
```

Without `PLAYWRIGHT_PORT=3001`, the full suite hits Docker instead of Next.js and shows ~71 spurious failures. In isolation with the correct port all specs pass.

## What Still Needs RTH Or Stronger Evidence

S5 (credit spread) and S8 (long option + two rules) require live chain quotes to open positions and are blocked until 09:30–16:00 ET. S2 also needs market marks to move. These can be verified:

- In the first RTH session on paper before live flip, OR
- Deferred to live burn-in at position size 1 contract

Do not treat those as passed until the paper evidence file contains the DB rows, event IDs, timestamps, and broker observations.

S4 has a clean-state GM pass in `docs/runbooks/position-rules-smoke-evidence-2026-05-07.md`: direct broker BUY 1 GM, `sweep --dry-run` returned only GM after the combo-leg filter, `sweep --apply` inserted rows 45/46, and daemon arming placed native STP `permId=789792324`. S6 has now been tightened with a fresh T native STP (`protection_id=31`, `permId=1661205040`) present before daemon restart and still present afterward with the same broker `permId`.

Health note: a later smoke pass found that daemon liveness could go red during quiet ticks with no rule transitions. The handler now emits scoped `position_rule.heartbeat` events and health uses heartbeat-or-transition max timestamp for `last_tick_at`.

S6 implementation update:

- `src/xenon/monitor_daemon/run.py` now connects the position-rules IB client at startup and calls `boot_reconcile()` once before registering/running the handler.
- Regression: `scripts/tests/test_monitor_daemon/test_run_setup.py::test_position_rules_startup_runs_boot_reconcile_after_connect`.
- Verification run: `uv run pytest scripts/tests/test_monitor_daemon/test_run_setup.py scripts/tests/test_position_rules_db/test_reconcile.py -q` → 7 passed.
- Paper restart evidence: `tmux kill-session -t xenon-paper-position-rules`, restart daemon, boot reconcile logged `{'claims_resolved': 0, 'armed_rows_re_armed': 0, 'armed_rows_canceled': 0}`, and IB open orders before/after showed one `SELL 1x T [STP]` with `permId=1661205040` / `orderRef='xenon-pr-native-31'`.

## Live DB State Left Behind

```
xenon.position_protection (DUQ378889, non-terminal):
  protection_id=11  stop_loss   CLOSED  STK::SPY  (finished, no action needed)
  protection_id=31  stop_loss   ARMED   STK::T    perm_id=1661205040
  protection_id=32  trailing_tp ARMED   STK::T    (alert-only, no native STP)
```

If you want a clean state before live flip, cancel protection_id=31 and protection_id=32:

```bash
XENON_TRADING_MODE=paper XENON_BROKER_ACCOUNT=DUQ378889 XENON_BROKER=IB \
  uv run xenon-position-rules cancel 31
XENON_TRADING_MODE=paper XENON_BROKER_ACCOUNT=DUQ378889 XENON_BROKER=IB \
  uv run xenon-position-rules cancel 32
```

## Bugs Fixed During Smoke

Present in the current worktree/branch:

| Bug                                            | File                                       | Symptom                                                            |
| ---------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------ |
| IB error 399 fatal → 502 on outsideRth LMT     | `src/xenon/execution/ib_place_order.py`    | Orders placed after hours returned 502; fix: treat 399 as advisory |
| IB error 2109 fatal → 502 on MKT outsideRth    | same                                       | Same fix path; 2109 added to ignore list                           |
| `_positions_from_ib()` drops contract fields   | `src/xenon/cli/position_rules.py`          | OPT positions keyed as STK::SYM; anchor_price=0.0 for all sweeps   |
| `sweep_insert()` anchor_price=0.0 without mark | `src/xenon/execution/brackets/arm_hook.py` | Stop threshold at 0→always triggered immediately                   |
| `sweep --apply` negative skipped count         | `src/xenon/cli/position_rules.py`          | One candidate inserting two rows printed `skipped=-1`              |

Regression tests for all five: green.

## Merge Checklist

Before opening the PR for merge to master:

1. **S2 verified** — `state_data.mfe` moves with favorable marks and no premature trigger occurs.
2. **S4 tightened** — clean-state TWS-direct sweep evidence captured.
3. **S5/S8 either completed in RTH paper or explicitly deferred by operator sign-off.**
4. **Python tests green:** `uv run pytest scripts/tests/ -x`
5. **Vitest green:** `cd web && npm test`
6. **Playwright position-rules spec:** `PLAYWRIGHT_PORT=3001 npx playwright test e2e/positionRules.spec.ts`
7. **No new E2E regressions:** `PLAYWRIGHT_PORT=3001 npx playwright test` (expect same pre-existing failures as on master: `account-metric-cards`, `modify-order-spread-telemetry`, `spread-price-bar`)
8. PR via `gh pr create` — never push directly to master.

## Key Files

| File                                                        | Purpose                    |
| ----------------------------------------------------------- | -------------------------- |
| `src/xenon/execution/brackets/arm_hook.py`                  | Core fill→arm logic        |
| `src/xenon/monitor_daemon/handlers/position_rules.py`       | Daemon tick handler        |
| `src/xenon/cli/position_rules.py`                           | `xenon-position-rules` CLI |
| `src/xenon/api/services/position_rules_cancel.py`           | API cancel service         |
| `web/components/portfolio/ShieldBadge.tsx`                  | Per-position badge         |
| `web/components/portfolio/PositionRulesDrawer.tsx`          | Drawer                     |
| `web/components/portfolio/GlobalHealthIndicator.tsx`        | Sidebar health dot         |
| `web/e2e/positionRules.spec.ts`                             | Playwright smoke           |
| `docs/runbooks/position-rules-smoke-evidence-2026-05-07.md` | Full evidence              |
| `docs/runbooks/position-rules-paper-smoke.md`               | Runbook (checklist)        |
