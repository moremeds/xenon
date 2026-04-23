# Loose Ends — 2026-04-23

Snapshot after archiving 11 shipped plans/specs from the Order Execution
Foundation program (F0–F7), the Futu ticker-chain fixes, the position-row
order button, the position-order modal rework, and the 2026-04-22 test
triage. Archive lives in `docs/{plans,superpowers/{plans,specs}}/archive/`.

## Ranked follow-ups

### P0

1. **Position-order modal follow-up triage.** _(resolved 2026-04-23)_
   PR #33 shipped both `position-row-order-button.md` and
   `position-order-modal-rework.md`. The safety item is now shipped on
   branch `feat/position-order-quote-token` (P1 #1 below); the feature
   backlog stays at P3 #1.

### P1

1. **`quote_token` integration for position close/add.** _(shipped
   2026-04-23, awaiting burn-in)_ Spec
   `docs/superpowers/specs/2026-04-23-position-order-quote-token-design.md`
   - plan `docs/superpowers/plans/2026-04-23-position-order-quote-token.md`.
     End-to-end: `ib_sync` emits `conId` per leg → `PortfolioLeg` +
     `TicketPayload` carry it → `useQuoteTokens` mints N parallel tokens →
     `PositionOrderModal` attaches `quote_token` (single) or `quote_tokens`
     (combo) → Next.js route forwards → FastAPI runs `quote_guard.check_combo`
     on combos, records `QUOTE_CHECK_PASS` / `QUOTE_TOKEN_MISSING_SOFT`
     telemetry. Missing tokens on combo soft-fail during one-week burn-in,
     then flip to hard (see new P2 #3 below).

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
   `docs/plans/2026-04-22-memory-usage-performance-report.md` flagged
   unbounded caches and report-path materialization. `uw_analyze_cache`
   already has entry caps, so no incident. Pick the next-biggest offender
   and cap it; skip the rest until RSS pressure returns.

3. **Flip combo `quote_tokens` missing → hard-reject.**
   After the `feat/position-order-quote-token` branch merges, watch
   `orders_events` for one burn-in week. If zero `QUOTE_TOKEN_MISSING_SOFT`
   rows originate from web clients, remove the soft-fail branch in
   `src/xenon/api/server.py` combo path and require `quote_tokens` in
   schema. Trivial one-line PR.

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
  (awaiting W1 plan), `docs/plans/2026-04-22-memory-usage-performance-report.md`
  (report, no owner).
