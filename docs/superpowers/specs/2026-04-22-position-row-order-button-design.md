# Position-row order button (IB tab)

**Status:** design  
**Date:** 2026-04-22  
**Scope:** `web/` only (no backend changes)

## Motivation

The Portfolio IB tab today supports order entry only via the Ticker link / instrument detail modal. Common position-level actions — especially closing an existing structure — require a detour through a ticker page and a fresh order build, even though the target order is a direct function of the current position. Adding a single affordance on the row that opens a pre-filled ticket saves the user from re-entering data they're already looking at.

## Goals

- One-click access to a close ticket for any IB position, from the IB tab row.
- Preset-tile UX that makes room for future actions (trailing SL/TP, roll) without redesigning.
- Zero changes to the broker-facing order pipeline — reuse `/api/orders/place` and existing validation.
- Preserve Gate 4 (no naked shorts): button never appears on Futu rows, and the Close preset is incapable of opening new short exposure.

## Non-goals

- TRAIL / TRAIL LMT order-type support. Tracked as a separate spec; buttons appear disabled with "coming soon" tooltip.
- Roll logic (restructuring / credit-debit reasoning across expiries). Tracked as a separate spec; button appears disabled with "coming soon" tooltip.
- Per-leg partial closes on combos. The existing `InstrumentDetailModal` already covers that path.

## UX

### Affordance

A small `⚡` icon button renders in the ticker cell of each IB position row, immediately after the existing expand-legs chevron. Visible on every IB row regardless of whether the position is single-leg or combo. Not rendered on Futu rows (`readonly={true}`).

### Modal

Click → opens `PositionOrderModal`, a portal based on the existing `Modal` primitive. The modal contains:

- **Preset tile bar** — four segmented tiles across the top:
  - `Close` — active, default selected
  - `Trailing Stop Loss` — disabled, tooltip: "Coming soon — requires TRAIL order support"
  - `Trailing Take Profit` — disabled, same tooltip
  - `Roll` — disabled, tooltip: "Coming soon — restructuring ticket in follow-up spec"
- **Close ticket form** — reuses `OrderPriceStrip`, `OrderLegPills`, and `ModifyOrderQuoteTelemetry` from the existing modify modal so the surface is visually identical.

Escape / backdrop click / Cancel button dismiss. Enter submits. Submission calls `/api/orders/place` and closes the modal on success, surfacing a toast with the order id.

### Close ticket prefill

**Direction mapping**

| Position             | Close action                                                                                                                                                                                      |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- | -------- |
| LONG stock           | SELL, qty = `contracts` shares                                                                                                                                                                    |
| SHORT stock          | BUY, qty = `                                                                                                                                                                                      | contracts | ` shares |
| LONG option (1 leg)  | SELL-TO-CLOSE                                                                                                                                                                                     |
| SHORT option (1 leg) | BUY-TO-CLOSE                                                                                                                                                                                      |
| Combo (N ≥ 2 legs)   | BAG order, `Order.action` derived from net direction of the structure; per-leg `ComboLeg.action` follows the existing convention (LONG leg = BUY, SHORT leg = SELL, regardless of `Order.action`) |

Net direction of a combo is determined by inspecting leg signs from `PortfolioPosition.legs`, not by re-deriving from P&L or market value.

**Quantity**

- Default = `pos.contracts` (full position).
- Three quick-pick chips: `[100%]` `[50%]` `[25%]`. Chip sets qty = `max(1, round(pos.contracts × pct))`.
- Qty is editable as a number input.
- When qty < pos.contracts, an inline note reads: `Partial close — N of M contracts`.

**Price**

- Default = net mid via `computeNetOptionQuote()` for combos, `prices[key].last`-fallback-mid for single-leg option, `prices[ticker].last` for stock.
- BID / MID / ASK quick-set buttons (inherited from `OrderPriceStrip`).
- Order type fixed at LMT.

**Submit**

- POST `/api/orders/place` with the payload shape already used by `OrderTab.tsx` and `ComboOrderForm`. No new backend route.
- Four Gates validation is unchanged. Gate 4 (naked short) is trivially satisfied: a close can only reduce short exposure.
- On success, modal closes and a toast displays the order id.

## Architecture

### New files

- `web/components/PositionOrderModal.tsx` — Props: `{ position: PortfolioPosition, prices: Record<string, PriceData>, onClose: () => void, onSubmitted?: (orderId: string) => void }`. Owns `activePreset` state.
- `web/lib/positionOrderPresets.ts` — Pure functions, no React:
  - `buildCloseTicket(position, prices): CloseTicketDraft` — returns a draft matching `/api/orders/place` payload shape.
  - `CloseTicketDraft` — TypeScript type.
- `web/tests/position-order-close-preset.test.ts` — unit tests for preset logic.
- `web/tests/position-order-modal.test.tsx` — component tests.

### Modified files

- `web/components/PositionTable.tsx`:
  - Add `⚡` button in the ticker cell (inside existing `ticker-with-chevron` span), gated on `!readonly`.
  - Hoist `activeOrderPosition: PortfolioPosition | null` state into the `PositionTable` component, mirroring the `activeInstrument` pattern used for `InstrumentDetailModal`.
  - Render `PositionOrderModal` when `activeOrderPosition` is set and `!readonly`.
- `web/tests/position-table-readonly.test.tsx` — extend to assert the new button is not rendered when `readonly={true}`.

### No changes

- No FastAPI or Python changes.
- No `ModifyOrderModal` changes — its primitives are reused by importing, not by forking.
- No routing changes.

## Testing

### Unit — `position-order-close-preset.test.ts`

- LONG stock → SELL, qty preserved.
- SHORT stock → BUY.
- LONG single-leg call → SELL-TO-CLOSE.
- SHORT single-leg put → BUY-TO-CLOSE.
- Bull call spread (LONG low-C / SHORT high-C) → BAG with `Order.action = SELL`, per-leg `action` = `BUY / SELL` (no double-reversal — load-bearing regression guard for IB error 201).
- Iron condor (4 legs, 2 LONG / 2 SHORT) → BAG with correct per-leg signs.
- Qty chip math: `contracts = 7` → chips produce `7 / 4 / 2` (round-half-up, min 1).
- Zero-qty guard: any chip yielding `0` clamps to `1`.
- Price defaults: net mid via `computeNetOptionQuote` for combos; single-leg uses `prices[key].last` with bid/ask mid fallback.

### Component — `position-order-modal.test.tsx`

- Button renders on `readonly=false` rows, absent on `readonly=true` rows.
- Click opens the modal with the expected position.
- All four preset tiles render; Close is active; others are `aria-disabled` with the expected tooltip text.
- Escape and backdrop click close the modal.
- Submit calls `/api/orders/place` with the expected payload (fetch mocked).

### E2E — chrome-cdp (per `web/CLAUDE.md` UI-verification rule)

- `npm run dev`, open Portfolio → IB tab, confirm ⚡ button on a real position row.
- Click → modal opens with Close tile highlighted; hover disabled tiles to verify tooltip.
- 50% chip → verify Partial-close note.
- Submit in test mode (`XENON_API_TEST_MODE` via `web/tests/fastapiHarness.ts`) and confirm the order surfaces in the Orders tab.
- Switch to Futu tab, confirm no ⚡ buttons anywhere (Gate 4 safety regression guard).

## Risks

- **Combo leg sign** is the load-bearing piece. Double-reversal produces IB error 201 — visible failure, not silent — but the unit tests covering every combo shape in `docs/trading/options-structures.json` are mandatory before merge.
- **Partial closes on spreads** can leave an imbalanced structure mid-fill if IB only fills one leg. This is the same behavior as the existing order path — no new risk introduced, but documented here so the follow-up Roll spec inherits the concern.
