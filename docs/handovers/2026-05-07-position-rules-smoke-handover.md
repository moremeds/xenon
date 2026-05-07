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
| S1  | Stock SL+TP arming      | **Not complete** — SPY qty=100 fill not received      |
| S2  | Trailing TP MFE         | **Not verified** — needs fill plus mark movement      |
| S3  | Manual TWS cancel       | **Complete**                                          |
| S4  | Sweep CLI re-arm        | **Partial** — functional evidence, exact origin weak  |
| S5  | Credit spread           | **Skipped** — RTH required                            |
| S6  | Daemon kill+restart     | **Complete** — same live STP survived daemon restart  |
| S7  | Native+synthetic race   | **Complete** by allowed integration-test fallback     |
| S8  | Two rules same position | **Skipped** — RTH required                            |
| S9  | Subprocess timeout      | **Complete** by allowed integration-test fallback     |
| S10 | OOB sweep at close      | **Complete**                                          |
| S11 | UI                      | **Complete for core UI**; DLQ red not manually smoked |

**Strict count:** 6/11 complete: S3, S6, S7, S9, S10, and S11 core UI. Treat S4 as partial until stronger evidence is captured.

## Immediate Task: Verify S1 Fill

A BUY 100 SPY LMT $734.00 outsideRth=true order (permId=1661205036) was placed and is queued for 04:00 AM ET pre-market on 2026-05-07. **Before doing anything else, check if it filled:**

Last verified at 2026-05-07 01:59 ET:

- IB open orders: `BUY 100x SPY @ $734.0 [LMT] — Submitted`
- DB order trail: `submission_id=437b5cbf-b228-4eab-8ff9-9968e8430647`, `perm_id=1661205036`, `ib_order_id=13`, `placing_client_id=26`, `state=WORKING`
- DB fills: no `SPY` qty=100 row for `broker_account='DUQ378889'`

Paper monitor daemon was restarted in detached tmux session `xenon-paper-position-rules` at 2026-05-07 14:12 HKT:

```bash
tmux capture-pane -pt xenon-paper-position-rules -S -80
tmux attach -t xenon-paper-position-rules
tmux kill-session -t xenon-paper-position-rules
```

Startup evidence in that pane shows `position_rules boot reconcile completed`, `EventSubscriber listening on ['fill.recorded']`, and the same SPY order `permId=1661205036` as `Submitted`.

```bash
PGPASSWORD=xenon_dev psql -h 127.0.0.1 -U xenon_app -d core_dev -c \
  "SELECT exec_id, qty, price, filled_at FROM xenon.order_fills
   WHERE ticker='SPY' AND broker_account='DUQ378889' AND qty=100
   ORDER BY filled_at DESC LIMIT 3"
```

If a qty=100 fill appears, check whether `arm_hook` fired:

```bash
PGPASSWORD=xenon_dev psql -h 127.0.0.1 -U xenon_app -d core_dev -c \
  "SELECT protection_id, rule_kind, state, native_order_perm_id
   FROM xenon.position_protection
   WHERE broker_account='DUQ378889' AND position_key='STK::SPY'
   ORDER BY protection_id DESC LIMIT 6"
```

**If the fill exists but no new PENDING_ARM/ARMED rows appeared for the SPY 100-share position** (protection_ids > 28), the daemon was likely not running when FastAPI emitted the NOTIFY. To trigger `arm_hook` retroactively, start FastAPI (port 8323, paper mode) and the monitor daemon. The fills replay on boot should re-insert the fill, and the daemon's arm_consumer should pick up the NOTIFY.

Start services:

```bash
cd /Users/chenxi/projects/xenon/.worktrees/position-rules-implementation
scripts/infra/dev.sh paper   # sets IB_GATEWAY_HOST=127.0.0.1:4002, runs alembic, starts FastAPI+Next.js
# In a separate shell:
XENON_TRADING_MODE=paper XENON_BROKER_ACCOUNT=DUQ378889 XENON_BROKER=IB \
  uv run xenon-monitor-daemon --daemon --ignore-market-hours
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

S4 needs one clean-state run where the position is opened directly in TWS paper, then discovered by `xenon-position-rules sweep --apply`. S6 has now been tightened with a fresh T native STP (`protection_id=31`, `permId=1661205040`) present before daemon restart and still present afterward with the same broker `permId`.

S6 implementation update:

- `src/xenon/monitor_daemon/run.py` now connects the position-rules IB client at startup and calls `boot_reconcile()` once before registering/running the handler.
- Regression: `scripts/tests/test_monitor_daemon/test_run_setup.py::test_position_rules_startup_runs_boot_reconcile_after_connect`.
- Verification run: `uv run pytest scripts/tests/test_monitor_daemon/test_run_setup.py scripts/tests/test_position_rules_db/test_reconcile.py -q` → 7 passed.
- Paper restart evidence: `tmux kill-session -t xenon-paper-position-rules`, restart daemon, boot reconcile logged `{'claims_resolved': 0, 'armed_rows_re_armed': 0, 'armed_rows_canceled': 0}`, and IB open orders before/after showed one `SELL 1x T [STP]` with `permId=1661205040` / `orderRef='xenon-pr-native-31'`.

## Live DB State Left Behind

```
xenon.position_protection (DUQ378889, non-terminal):
  protection_id=11  stop_loss   CLOSED  STK::SPY  (finished, no action needed)
  protection_id=30  trailing_tp ARMED   STK::T    (alert-only, no native STP)
```

If you want a clean state before live flip, cancel protection_id=30:

```bash
XENON_TRADING_MODE=paper XENON_BROKER_ACCOUNT=DUQ378889 XENON_BROKER=IB \
  uv run xenon-position-rules cancel 30
```

## Bugs Fixed During Smoke

Present in the current worktree/branch:

| Bug                                            | File                                       | Symptom                                                            |
| ---------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------ |
| IB error 399 fatal → 502 on outsideRth LMT     | `src/xenon/execution/ib_place_order.py`    | Orders placed after hours returned 502; fix: treat 399 as advisory |
| IB error 2109 fatal → 502 on MKT outsideRth    | same                                       | Same fix path; 2109 added to ignore list                           |
| `_positions_from_ib()` drops contract fields   | `src/xenon/cli/position_rules.py`          | OPT positions keyed as STK::SYM; anchor_price=0.0 for all sweeps   |
| `sweep_insert()` anchor_price=0.0 without mark | `src/xenon/execution/brackets/arm_hook.py` | Stop threshold at 0→always triggered immediately                   |

Regression tests for all four: green.

## Merge Checklist

Before opening the PR for merge to master:

1. **S1 fill confirmed** — qty=100 SPY fill appears in `order_fills`, and ARMED stop_loss row exists with valid `native_order_perm_id`.
2. **S2 verified** — `state_data.mfe` moves with favorable marks and no premature trigger occurs.
3. **S4 tightened** — clean-state TWS-direct sweep evidence captured.
4. **S5/S8 either completed in RTH paper or explicitly deferred by operator sign-off.**
5. **Python tests green:** `uv run pytest scripts/tests/ -x`
6. **Vitest green:** `cd web && npm test`
7. **Playwright position-rules spec:** `PLAYWRIGHT_PORT=3001 npx playwright test e2e/positionRules.spec.ts`
9. **No new E2E regressions:** `PLAYWRIGHT_PORT=3001 npx playwright test` (expect same pre-existing failures as on master: `account-metric-cards`, `modify-order-spread-telemetry`, `spread-price-bar`)
10. PR via `gh pr create` — never push directly to master.

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
