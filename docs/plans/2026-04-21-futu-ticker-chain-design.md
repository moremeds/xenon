# Futu Ticker Navigation And Chain Reliability Design

**Date:** 2026-04-21

**Status:** Approved

## Goal

Fix the current UX gap where a ticker in the Futu portfolio cannot open the ticker workspace, while also hardening the options chain so same-day expiries can be selected and loaded, and Delta / IV render when the live option payload contains them.

## Scope

In scope:

- Allow navigation from Futu portfolio rows to `/{TICKER}`.
- Preserve current lack of Futu order entry / execution support.
- Keep direct typo-style routes such as `/appl` out of scope.
- Keep the chain default expiry behavior focused on dated expiries.
- Ensure 0DTE expiries remain selectable and load correctly when chosen.
- Ensure chain Delta / IV cells show live values when present and remain blank when absent.

Out of scope:

- Adding any Futu execution, order entry, or broker integration.
- Redesigning ticker routing beyond the agreed navigation change.
- Adding a fallback data source for Greeks or IV.
- Changing the direct-route behavior for invalid or mistyped symbols.

## Agreed Product Behavior

### 1. Futu Navigation

Futu portfolio rows should open the same ticker workspace used by IB rows. This is a navigation-only change. Opening a ticker from the Futu side must not imply Futu-backed execution support.

### 2. Direct Ticker Routes

The direct route case the user mentioned, such as `/appl`, is treated as a separate issue and remains untouched in this design.

### 3. 0DTE Chain Selection

When the expirations API returns a same-day expiry plus later dated expiries:

- the Chain tab should keep its current preference for a later dated default expiry,
- the same-day expiry must still appear in the selector,
- choosing the same-day expiry must fetch and render its strikes normally.

### 4. Delta / IV Display

The chain UI already has Delta and IV columns. The intended behavior is:

- render Delta and IV when the option quote payload includes `delta` and `impliedVol`,
- render blank cells when those fields are `null` or unavailable,
- do not add any fallback data source.

## Recommended Approach

Use a surgical fix on the existing surfaces.

Why this approach:

- It matches the requested scope.
- It avoids inventing a new account-aware routing model.
- It preserves the current broker boundary: Futu can be observed, not traded.
- It keeps the regression surface small enough for strong unit and browser coverage.

Rejected alternatives:

- Adding explicit `account=futu` route state through the full ticker page. This is clearer architecturally but broader than needed.
- Reworking the full ticker route and account model in one pass. This is disproportionate to the approved scope.

## Implementation Shape

### Futu Navigation

The current Futu table path intentionally disables ticker navigation. That behavior will be revised so Futu rows can navigate to `/{TICKER}` while staying within the current non-execution posture.

Primary touch points:

- `web/components/TickerLink.tsx`
- `web/components/PositionTable.tsx`
- any tests that currently assert Futu ticker cells are non-interactive

### Chain Expiry Handling

The likely failure is the initial selection policy in `OptionsChainTab`: it prefers the first expiry whose `daysToExpiry(expiry) >= 7`. That is acceptable for the default, but it must not make 0DTE unselectable or unloadable.

Primary touch points:

- `web/components/ticker-detail/OptionsChainTab.tsx`
- `web/lib/optionsChainUtils.ts` if date normalization or day-count logic needs tightening

### Delta / IV Rendering

The UI already reads `delta` and `impliedVol` from `PriceData`. The work here is to verify and preserve the data path from live option subscription updates into the chain table so these fields appear when present and remain blank when absent.

Primary touch points:

- `web/lib/usePrices.ts`
- `web/components/ticker-detail/OptionsChainTab.tsx`
- any chain E2E fixtures that currently omit or incorrectly shape those fields

## Testing Strategy

Project rules require TDD for bug fixes and browser verification for UI behavior.

### Futu Navigation Tests

- Add or update unit coverage around `TickerLink` / readonly navigation behavior.
- Replace the current E2E expectation that Futu ticker cells must be non-interactive.
- Add browser coverage that:
  - switches to the Futu account tab,
  - clicks a ticker,
  - verifies navigation to `/{TICKER}`,
  - confirms no new Futu execution path was introduced as part of this change.

### 0DTE Chain Tests

- Add a failing unit or component-level regression where expirations include today plus later dates.
- Verify the default selected expiry stays on the dated expiry.
- Verify selecting the same-day expiry loads its strikes.
- Add browser coverage for the selector flow.

### Delta / IV Tests

- Add a failing regression that streams option quote updates containing `delta` and `impliedVol`.
- Verify the chain table renders those values.
- Add the null case and verify the cells stay blank instead of showing placeholders or stale values.

## Risks

- Existing Futu read-only tests will fail until updated to the new contract.
- If the chain issue is partly due to backend expiry formatting, the frontend fix may expose a second normalization bug.
- If option subscription fixtures differ between tests and runtime, Greeks / IV may appear fixed in unit tests but remain absent in browser tests without realistic websocket payloads.

## Verification Before Completion

Before implementation can be called done:

- unit tests for the touched behavior must pass,
- browser tests covering Futu navigation and chain expiry selection must pass,
- the rendered UI must be visually checked in a browser per project instructions.
