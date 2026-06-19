# Futu Ticker Navigation And Chain Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let Futu portfolio rows navigate to ticker pages, keep 0DTE expiries selectable and loadable without becoming the default, and restore live Delta / IV values in the chain by fixing the app-side option quote path that drops or fails to surface those values.

**Architecture:** Keep the fix narrow. Update the Futu-specific ticker navigation contract in the web layer, adjust chain expiry selection behavior only if the manual 0DTE path is proven broken, and trace the blank Delta / IV problem through the realtime relay so the fix is chosen from evidence rather than assumed implementation shape. Validate each area with red/green tests first, then browser verification.

**Tech Stack:** Next.js 16, React 19, Vitest, Playwright, Node IB realtime relay in `scripts/infra/ib_realtime`, TypeScript, zsh.

---

### Task 1: Replace The Futu Ticker Navigation Gate With “navigable but non-executable”

**Files:**
- Modify: `web/components/TickerLink.tsx`
- Modify: `web/components/PositionTable.tsx`
- Modify: `web/tests/position-table-readonly.test.tsx`
- Modify: `web/e2e/futu-readonly.spec.ts`

**Step 1: Write the failing unit expectation for the Futu navigation contract**

Update `web/tests/position-table-readonly.test.tsx` so the test stops treating “readonly” as “must not navigate” and instead encodes the new intended contract:
- ticker rows remain navigable,
- modal / execution surfaces remain gated in readonly mode.

If the current file is too coupled to the old contract, split the assertions into:
- navigation behavior in `TickerLink` / `PositionTable`,
- modal safety in the existing readonly test.

Example assertion target:

```tsx
const tickerButtons = container.querySelectorAll("button.ticker-link");
expect(tickerButtons.length).toBeGreaterThan(0);
expect(container.querySelectorAll(".ticker-link-disabled").length).toBe(0);
```

**Step 2: Run the unit test to verify it fails**

Run:

```bash
cd web && npm test -- --run web/tests/position-table-readonly.test.tsx
```

Expected: FAIL because the current implementation still renders `span.ticker-link-disabled`.

**Step 3: Write the failing browser expectation for Futu navigation**

Update `web/e2e/futu-readonly.spec.ts` so it:
- expects a visible `button.ticker-link` for `TSLA` on the Futu tab,
- clicks it,
- waits for `**/TSLA`,
- still asserts no order placement request is made by this click path.

Keep the `/api/orders/place` route trap in place.

**Step 4: Run the browser test to verify it fails**

Run:

```bash
cd web && npm run test:e2e -- futu-readonly.spec.ts
```

Expected: FAIL because the Futu row is still rendered as a disabled span and no navigation occurs.

**Step 5: Write the minimal implementation**

In `web/components/TickerLink.tsx`:
- stop using the current disabled-span rendering for the Futu ticker path,
- keep rendering a navigable button,
- if `readonly` is now overloaded, split the prop or behavior so “cannot execute” is not implemented as “cannot navigate.”

In `web/components/PositionTable.tsx`:
- keep `readonly` gating for modal/order behavior,
- do not use the Futu safety gate to suppress ticker navigation.

**Step 6: Run the targeted tests to verify they pass**

Run:

```bash
cd web && npm test -- --run web/tests/position-table-readonly.test.tsx
cd web && npm run test:e2e -- futu-readonly.spec.ts
```

Expected: PASS.

**Step 7: Commit**

```bash
git add web/components/TickerLink.tsx web/components/PositionTable.tsx web/tests/position-table-readonly.test.tsx web/e2e/futu-readonly.spec.ts
git commit -m "fix: allow futu ticker navigation"
```

### Task 2: Reproduce The Exact 0DTE Failure Before Changing Chain Logic

**Files:**
- Modify: `web/components/ticker-detail/OptionsChainTab.tsx`
- Create: `web/tests/options-chain-0dte-selection.test.tsx`
- Create: `web/e2e/chain-0dte-selection.spec.ts`

**Step 1: Write the failing browser regression for the user-reported behavior**

Create `web/e2e/chain-0dte-selection.spec.ts` first. The browser test should prove the exact user-visible problem:
- expirations include today plus later dates,
- the default selected expiry is not 0DTE,
- selecting 0DTE manually either fails to refresh the chain or shows the wrong strikes.

Use distinct mocked strike sets per expiry so the result is unambiguous.

**Step 2: Run the browser test to verify it fails**

Run:

```bash
cd web && npm run test:e2e -- chain-0dte-selection.spec.ts
```

Expected: FAIL on the manual 0DTE selection flow if the user-reported bug is reproducible.

**Step 3: Add the narrower component-level regression**

Create `web/tests/options-chain-0dte-selection.test.tsx` to mirror only the failing behavior confirmed by the browser test. If the only confirmed issue is default selection, test that. If manual selection also fails, test that exact refresh path too.

Mock `fetch`, `useTickerDetail`, and minimal price state as needed.

**Step 4: Run the unit test to verify it fails**

Run:

```bash
cd web && npm test -- --run web/tests/options-chain-0dte-selection.test.tsx
```

Expected: FAIL on the exact behavior reproduced in Step 1.

**Step 5: Write the minimal implementation**

In `web/components/ticker-detail/OptionsChainTab.tsx`:
- keep the current non-0DTE default policy unless the browser repro proves that is wrong,
- fix only the failing same-day path that was reproduced,
- if selector changes are the problem, ensure fetch/cache/state are keyed by the actual selected expiry,
- keep expiry normalization consistent with `YYYYMMDD`.

Do not move the default to 0DTE.

**Step 6: Run the targeted tests to verify they pass**

Run:

```bash
cd web && npm test -- --run web/tests/options-chain-0dte-selection.test.tsx
cd web && npm run test:e2e -- chain-0dte-selection.spec.ts
```

Expected: PASS.

**Step 7: Commit**

```bash
git add web/components/ticker-detail/OptionsChainTab.tsx web/tests/options-chain-0dte-selection.test.tsx web/e2e/chain-0dte-selection.spec.ts
git commit -m "fix: support selectable 0dte chains"
```

### Task 3: Reproduce Blank Delta / IV At The Relay Boundary Before Choosing The Fix

**Files:**
- Create: `web/e2e/chain-greeks-display.spec.ts`
- Modify: `web/e2e/ticker-search-chain.spec.ts` if a shared helper is worth reusing

**Step 1: Write the failing browser regression**

Create `web/e2e/chain-greeks-display.spec.ts` that:
- navigates to `/AAPL?tab=chain`,
- mocks expirations and strikes,
- installs a websocket mock that emits option updates containing non-null `delta` and `impliedVol`,
- asserts the corresponding chain cells render those values,
- adds a second assertion path where `delta` and `impliedVol` are null and the cells remain blank.

Use explicit option keys like `AAPL_20260417_200_C`.

**Step 2: Run the browser test to verify it fails**

Run:

```bash
cd web && npm run test:e2e -- chain-greeks-display.spec.ts
```

Expected: FAIL because the current live path is not surfacing Delta / IV into the rendered chain cells consistently.

**Step 3: Add a relay-boundary regression instead of a source-shape assertion**

Create `web/tests/ib-option-greeks-path.test.ts` that asserts one concrete behavioral property of the relay path. Acceptable targets:
- option subscription setup preserves the normalized `SYMBOL_YYYYMMDD_STRIKE_RIGHT` key across request / update / broadcast,
- `tickOptionComputation` updates on an option symbol are hydrated and broadcast with the same key the web chain subscribes to,
- a dedicated helper exists for preparing live option subscriptions and is called from the option-subscription block.

Do not write a test that merely forbids one source string. The test must protect behavior, not implementation style.

**Step 4: Run the unit test to verify it fails**

Run:

```bash
cd web && npm test -- --run web/tests/ib-option-greeks-path.test.ts
```

Expected: FAIL on the relay behavior you chose to protect.

### Task 4: Fix The Realtime Option Quote Path Based On The Reproduced Failure

**Files:**
- Modify: `scripts/infra/ib_realtime/ib_realtime_server.js`
- Modify: `web/tests/ib-index-stream-contracts.test.ts`
- Modify: `web/tests/ib-option-greeks-path.test.ts`
- Optionally create: `scripts/infra/ib_realtime/ib_realtime_server.test.js` if a pure helper extraction is cleaner

**Step 1: Add the failing unit assertion for the actual relay failure**

Extend the relay tests so they require the concrete behavior established in Task 3.

**Step 2: Run the unit tests to verify failure**

Run:

```bash
cd web && npm test -- --run web/tests/ib-index-stream-contracts.test.ts web/tests/ib-option-greeks-path.test.ts
```

Expected: FAIL on the reproduced relay-path issue.

**Step 3: Write the minimal implementation**

In `scripts/infra/ib_realtime/ib_realtime_server.js`:
- fix the option quote path that is dropping or failing to surface Delta / IV,
- preserve the existing normalized symbol key used by the web client,
- avoid changing stock/index subscription behavior,
- if the evidence shows contract preparation is the issue, add the smallest qualification / preparation helper needed,
- if the evidence shows keying / restore / hydration is the issue, fix that instead.

Keep the broadcast symbol key as `SYMBOL_YYYYMMDD_STRIKE_RIGHT`.

**Step 4: Run the unit tests to verify they pass**

Run:

```bash
cd web && npm test -- --run web/tests/ib-index-stream-contracts.test.ts web/tests/ib-option-greeks-path.test.ts
```

Expected: PASS.

**Step 5: Run the browser regression from Task 3**

Run:

```bash
cd web && npm run test:e2e -- chain-greeks-display.spec.ts
```

Expected: PASS, with live Delta / IV rendered when present.

**Step 6: Commit**

```bash
git add scripts/infra/ib_realtime/ib_realtime_server.js web/tests/ib-index-stream-contracts.test.ts web/tests/ib-option-greeks-path.test.ts web/e2e/chain-greeks-display.spec.ts
git commit -m "fix: restore chain greek values"
```

### Task 5: Run Focused Regression Suite Across All Three Fixes

**Files:**
- No product code expected
- Update test files only if failures expose broken assumptions

**Step 1: Run targeted Vitest coverage**

Run:

```bash
cd web && npm test -- --run web/tests/position-table-readonly.test.tsx web/tests/options-chain-0dte-selection.test.tsx web/tests/ib-index-stream-contracts.test.ts web/tests/ib-option-greeks-path.test.ts
```

Expected: PASS.

**Step 2: Run targeted Playwright coverage**

Run:

```bash
cd web && npm run test:e2e -- futu-readonly.spec.ts chain-0dte-selection.spec.ts chain-greeks-display.spec.ts
```

Expected: PASS.

**Step 3: Fix any test-only brittleness**

If mocks or selectors are brittle:
- adjust only the tests or tiny supporting code,
- do not widen product scope,
- re-run the same targeted commands.

**Step 4: Commit if any follow-up test stabilization changed files**

```bash
git add web/tests web/e2e
git commit -m "test: stabilize ticker chain regressions"
```

### Task 6: Browser Verification Against A Running UI

**Files:**
- No product code expected unless visual verification finds a real bug

**Step 1: Start the web app**

Run:

```bash
cd web && npm run dev
```

Expected: Next app, IB realtime relay, and FastAPI start locally.

**Step 2: Verify Futu ticker navigation in the browser**

In Playwright or the preferred browser verification tool:
- open `/portfolio`,
- switch to the Futu tab,
- click a ticker,
- verify navigation lands on `/{TICKER}` and the ticker page renders.

**Step 3: Verify 0DTE selection visually**

- open `/{TICKER}?tab=chain`,
- confirm default expiry remains dated,
- choose the same-day expiry,
- confirm the strike grid refreshes.

**Step 4: Verify Delta / IV visually**

- with a chain that has live option quote updates,
- confirm Delta / IV cells show numeric values,
- confirm missing values still display as blank cells, not placeholders or stale data.

**Step 5: Commit only if visual verification required a real code fix**

```bash
git add <touched-files>
git commit -m "fix: align ticker chain behavior with visual verification"
```

### Task 7: Final Verification And Handoff

**Files:**
- Modify: `docs/status.md` only if the team expects a log entry for this work

**Step 1: Run the final targeted commands one more time**

Run:

```bash
cd web && npm test -- --run web/tests/position-table-readonly.test.tsx web/tests/options-chain-0dte-selection.test.tsx web/tests/ib-index-stream-contracts.test.ts web/tests/ib-option-greeks-path.test.ts
cd web && npm run test:e2e -- futu-readonly.spec.ts chain-0dte-selection.spec.ts chain-greeks-display.spec.ts
```

Expected: PASS.

**Step 2: Capture the final outcome**

Record:
- which tests passed,
- whether browser verification was completed,
- any residual risk, especially around IB-specific live data timing.

**Step 3: Final commit if docs changed**

```bash
git add docs/status.md
git commit -m "docs: note ticker chain fixes"
```
