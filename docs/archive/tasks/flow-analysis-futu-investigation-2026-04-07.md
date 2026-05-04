# Flow Analysis Futu Investigation

Date: 2026-04-07
Page checked: `http://localhost:3001/flow-analysis`
Method: Playwright UI inspection + code-path trace

## Summary

The `/flow-analysis` page is loading Futu account metadata, but the actual flow-analysis report is still sourced from the IB portfolio path.

This is why the page can show:

- `FUTU ... 27 pos`
- flow-analysis sections showing `9 POSITIONS`

Those `9` positions match the IB cache, not the Futu cache.

## Browser Evidence

Playwright captured the live page before it redirected to Clerk sign-in.

Observed on the rendered page:

- IB tab: `9 pos`
- FUTU tab: `27 pos`
- `Neutral / Low Signal`: `9 POSITIONS`
- flow-analysis rows: `GLD`, `QQQ`, `SPY`, `USO`

That row set matches the IB portfolio snapshot, not the Futu snapshot.

Matching local data files at the time of inspection:

- `data/portfolio.json`: `position_count = 9`
- `data/flow_analysis.json`: `positions_scanned = 9`
- `data/futu_portfolio.json`: `count = 27`

## Root Cause

The account switcher is only changing the portfolio data consumed by the shell and portfolio tables. The flow-analysis report is not account-aware.

### Frontend

`web/components/WorkspaceShell.tsx`

- Loads both `usePortfolio(...)` and `useFutuPortfolio(...)`
- Switches the `portfolio` prop based on `activeAccount`
- Passes `activeAccount` into `WorkspaceSections`

This part is working as designed.

`web/components/WorkspaceSections.tsx`

- `FlowSections()` calls `useFlowAnalysis(true)`
- It does not receive or use `activeAccount`
- So the flow-analysis panels always read the same shared endpoint/cache regardless of account tab

### Next route

`web/app/api/flow-analysis/route.ts`

- `GET` reads a single shared cache: `data/flow_analysis.json`
- `POST` calls FastAPI `/flow-analysis`
- No account/source parameter exists

### FastAPI route

`scripts/api/server.py`

- `POST /flow-analysis` runs `flow_analysis.py`
- No broker/account parameter is accepted

### Python analysis script

`scripts/flow_analysis.py`

- Hardcodes `PORTFOLIO = PROJECT_DIR / "data" / "portfolio.json"`
- `load_portfolio()` only reads `data/portfolio.json`
- It never reads `data/futu_portfolio.json`

That hardcoded file boundary is the root cause. Everything above it is effectively forced onto the IB snapshot.

## Secondary Observation

During Playwright verification, the page loaded and fetched:

- `/api/portfolio`
- `/api/futu/portfolio`
- `/api/flow-analysis`

Then the session redirected to Clerk sign-in, with CORS/CSP errors in the console around Clerk navigation/telemetry.

That auth issue is separate from the Futu-position population bug, but it makes browser verification brittle and should be cleaned up independently.

## Plan

1. Make flow analysis account-aware end-to-end.
   - Add an `account` or `broker` parameter (`ib` | `futu`) from the page down to the API.
   - `FlowSections` should consume `activeAccount`.
   - `useFlowAnalysis` should request `/api/flow-analysis?account=futu` when the Futu tab is active.

2. Split cache by source.
   - Do not reuse one shared `data/flow_analysis.json` for both brokers.
   - Use separate caches such as:
     - `data/flow_analysis.ib.json`
     - `data/flow_analysis.futu.json`
   - This avoids stale cross-account leakage when switching tabs.

3. Add a Python-side adapter for Futu positions before analysis.
   - `flow_analysis.py` currently expects positions with Xenon/IB-style fields like `ticker`, `direction`, and `structure`.
   - Futu cache rows are a different shape.
   - Introduce a small adapter that converts `data/futu_portfolio.json` rows into the same minimal analysis shape before classification.

4. Update the FastAPI and Next routes to preserve the account choice.
   - Next route forwards `account`
   - FastAPI route forwards `account`
   - Python runner selects the correct portfolio source

5. Add regression coverage.
   - Unit: Futu cache rows adapt into analyzable positions correctly
   - Unit: flow-analysis route selects the correct cache/source by account
   - UI/browser: `/flow-analysis` on FUTU shows a flow-analysis count derived from Futu positions, not IB
   - UI/browser: switching IB ↔ FUTU updates both the portfolio tables and the flow-analysis sections consistently

## Recommended Implementation Order

1. Add failing tests for account-aware route selection and Futu position adaptation
2. Add account-aware backend selection
3. Add frontend query wiring from active account
4. Add browser regression for IB/FUTU tab switching on `/flow-analysis`
5. Re-run Playwright visual verification against the live page

## Files Most Likely To Change

- `web/components/WorkspaceSections.tsx`
- `web/lib/useFlowAnalysis.ts`
- `web/app/api/flow-analysis/route.ts`
- `scripts/api/server.py`
- `scripts/flow_analysis.py`
- new or adjacent tests for route/script/UI coverage
