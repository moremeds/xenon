# Loose Ends — 2026-04-23

Snapshot after archiving 11 shipped plans/specs from the Order Execution
Foundation program (F0–F7), the Futu ticker-chain fixes, the position-row
order button, the position-order modal rework, and the 2026-04-22 test
triage. Archive lives in `docs/{plans,superpowers/{plans,specs}}/archive/`.

## Ranked follow-ups

### P0

1. **Position-order modal follow-up triage.**
   PR #33 shipped both `position-row-order-button.md` and
   `position-order-modal-rework.md`. What remains from the rework plan's
   "out of scope" list splits into one safety item and a feature backlog
   (see P1 #1 and P3 #1). Decision needed: do we ship the safety item
   standalone or bundle it with the first wizard PR?

### P1

1. **`quote_token` integration for position close/add.**
   The rework explicitly skipped this — v1 proceeds without a token and
   the `/orders/place` route accepts missing token. Consequence: position
   close/add orders bypass the F3 limit-band safety that every other
   submit path enforces. Needs per-leg `con_id` resolution then plugs into
   existing `quote_guard.check`. Estimate: ~1 day. File new plan when
   wizard work allows a pause.

2. **PR-C/D leftovers audit.**
   Memory note `project_pr_cd_handover.md` claims "options tick grid +
   UI QA" are still open past PR #29. Five-minute diff vs. memory to
   confirm what is actually left vs. stale memory, then either file a
   plan or retire the memory entry.

### P2

1. **`data/orders.json` deprecation.**
   PR-C/D plan said audit keeps reading JSON "until a separate deprecation
   PR." Dual-read works, no acute risk. Bundle with the next change that
   already touches `naked_short_audit.py`.

2. **Memory-report cache caps (one-pick).**
   `docs/plans/archive/2026-04-22-memory-usage-performance-report.md` flagged
   unbounded caches and report-path materialization. `uw_analyze_cache`
   already has entry caps, so no incident. Pick the next-biggest offender
   and cap it; skip the rest until RSS pressure returns.

### P3

1. **Position-order v2 features.**
   Trailing SL/TP, Roll, Covered-Call/Collar/Synthetic combo close,
   editable combo legs, `acknowledge_limit_override`. Pure feature
   backlog. Revisit after the wizard is live.

2. **BAG/combo server-side preflight.**
   TS guard still covers combo pre-submit. Combo volume is low. Defer
   until the wizard starts emitting BAG orders at scale.

3. **`/uw-analyze` holiday calendar.**
   ~1 day over-budget per US holiday (~9/yr). ~2.5% annual budget
   overrun. Not worth a calendar dependency today; reconsider only if
   UW call volume starts pressing the 20k/day ceiling.

## Notes

- **Burn-in gate waived** (2026-04-23). Author proceeding to wizard
  (W1+) work without the ≥7-day clean window the master plan called for.
  Foundation observability (`orders_events`, `REHYDRATE_*`, daily
  naked-short audit) keeps running but is no longer a blocking gate.
  Watch for defects in the first wizard PR that would have been caught
  by a clean burn-in window.
- Active plans remaining: `docs/superpowers/specs/2026-04-20-leg-wizard-design.md`
  (awaiting W1 plan), `docs/plans/archive/2026-04-22-memory-usage-performance-report.md`
  (report, no owner).
