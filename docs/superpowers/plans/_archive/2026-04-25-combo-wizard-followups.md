# Combo Wizard — Follow-up Verification List

**Branch closed:** `feat/combo-wizard` → `master` (PR #46), 2026-04-25
**Owner:** chenxi
**Next check-in:** 2026-04-27 (Mon) — scheduled agent

This is the deferred-work checklist after closing `feat/combo-wizard`. Items are
either runtime-verifications that need a live backend, or design work flagged
during the merge that we explicitly chose not to ship in this branch.

## P0 — Verify next Monday (2026-04-27)

### 1. Submit-order regression fix (commit `654d72d2`)

PR #47 introduced a `quote_token` gate in `OrderTab.tsx` that broke every
single-leg submit because no Python portfolio route populates `leg.conId`.
Reverted in commit `654d72d2` on `feat/combo-wizard` (no separate PR — folded
straight onto the feature branch).

**Verify on Monday:**

- [ ] Open ticker page → click position-row Order button → fill qty + limit →
      Place → Confirm → order submits without "Quote unavailable" error.
- [ ] Standalone new order on a ticker page (no position pre-selected) →
      submit succeeds.
- [ ] PositionOrderModal close-add flow → submit succeeds.
- [ ] No console errors related to `useQuoteToken` or `quote_token`.

If any of these fail, the revert was incomplete — check
`web/components/ticker-detail/OrderTab.tsx` and the place-order body for any
leftover `quote.token` references.

### 2. Combo wizard paper dry-run

Outstanding from `project_combo_wizard_tasks_1_5` memory. Tasks 1–5.5 shipped
but no end-to-end paper-account dry-run has been performed yet.

**Verify on Monday:**

- [ ] Open wizard on a real spread (bull call / bear put / iron condor) on
      paper account.
- [ ] Walk through QUOTE → PLACE → MONITOR → PROTECT → DONE.
- [ ] Confirm `protect.py` only fires on FILLED legs (not PARTIALLY_FILLED).
- [ ] Confirm session strip survives page reload (rehydrate path).

### 3. Web E2E fixmes (2 outstanding)

Memory: "2 web E2E fixmes pending live backend." Identify which specs and
either un-fixme them or document why they remain skipped.

- [ ] `grep -rn 'fixme\|TODO.*live' web/tests web/e2e` — list current state.
- [ ] Either re-enable against live backend, or convert to documented skip
      with reason.

## P1 — Design / cleanup (no deadline)

### 4. Universal sensitive-page auth gating

Per project memory `project_universal_auth_gating`: per-flow gates have caused
two regressions (#34, #47). Need a single gate that covers OrderTab +
PositionOrderModal + ModifyOrderModal + WizardModal — not a fourth per-flow
attempt.

**Design options** (pick one before any reintroduction of `quote_token`):

- **Backend gate** in `/api/orders/place` — only path that can't be bypassed,
  but requires conId-resolved portfolio data to exist.
- **React provider** wrapping the entire order surface — exposes a single
  `useOrderGate()` hook, fails open if unconfigured.
- **Route middleware** (Next.js) — gates at the API-proxy layer.

Ship a design doc before implementation. Reference the prior reverted attempts
(#34, #47) so we don't ship the same shape a third time.

### 5. Backend `leg.conId` population

The `con_id` payload enrichment in `OrderTab.tsx` (kept after the revert) is
currently a no-op because no Python portfolio route sets `leg.conId`.

- [ ] Find every place that constructs a `PortfolioLeg` (start at
      `src/xenon/api/routes/portfolio*.py`).
- [ ] Add `conId` from the qualified IB contract.
- [ ] Test with a live position that has both stock and option legs.

Once this lands, the universal gate (item 4) becomes feasible.

### 6. Orphan `useQuoteToken` hook + test

`web/components/ticker-detail/useQuoteToken.ts` and
`web/tests/quote-token-client.test.ts` no longer have any production caller.
Delete them as part of the universal-gate work (item 4) — keeping them now
would either confuse future readers or get re-imported into another
half-baked gate attempt.

### 7. Combo wizard Task 6

Outstanding per `project_combo_wizard_tasks_1_5`. Pull the Task 6 spec from
the design doc and decide whether it's a separate branch or rolls into the
universal-gate work.

## How this list closes

Each item gets a tickbox. When all P0 items pass on 2026-04-27, this doc
moves to `docs/plans/archive/` and the scheduled agent stops re-checking.
