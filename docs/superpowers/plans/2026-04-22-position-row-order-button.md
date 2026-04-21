# Position-row order button (IB tab) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a small ⚡ button on each IB-tab position row that opens a popup with preset order tickets (Close live; Trailing SL, Trailing TP, Roll disabled as "coming soon"). Close prefills a reversed-direction LMT order at net mid with full qty by default, with 100/50/25% qty quick-chips.

**Architecture:** One new modal component (`PositionOrderModal`) reuses visual primitives from `ModifyOrderModal` (Modal wrapper, `OrderPriceStrip`, `OrderLegPills`, quote telemetry). One new pure-logic module (`positionOrderPresets.ts`) builds the `/api/orders/place` payload from a `PortfolioPosition` — unit-testable in isolation. `PositionTable.tsx` gains a button in the ticker cell (gated on `!readonly`) that opens the modal. No backend changes — reuses `/api/orders/place` as-is.

**Tech Stack:** Next.js App Router, React 19, TypeScript, Vitest, Testing Library, chrome-cdp for E2E.

---

## File Structure

**New files:**

- `web/lib/positionOrderPresets.ts` — pure TS: `buildCloseTicket(position, prices)` returns a ready-to-POST payload for `/api/orders/place`.
- `web/components/PositionOrderModal.tsx` — modal with preset tiles and Close form; hosts qty chips, price strip, submit logic.
- `web/tests/position-order-close-preset.test.ts` — unit tests for preset logic across stock / single-leg / combo shapes.
- `web/tests/position-order-modal.test.tsx` — component tests for modal behavior and readonly gating.

**Modified files:**

- `web/components/PositionTable.tsx` — add `⚡` button in ticker cell, hoist `activeOrderPosition` state, render `PositionOrderModal`.
- `web/tests/position-table-readonly.test.tsx` — extend to assert ⚡ button is absent when `readonly={true}`.

---

## Task 1: `buildCloseTicket` for stock positions

**Files:**

- Create: `web/lib/positionOrderPresets.ts`
- Test: `web/tests/position-order-close-preset.test.ts`

- [ ] **Step 1: Write failing tests for stock paths**

Create `web/tests/position-order-close-preset.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { buildCloseTicket } from "@/lib/positionOrderPresets";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

function stockPos(
  overrides: Partial<PortfolioPosition> = {},
): PortfolioPosition {
  return {
    id: 1,
    ticker: "TSLA",
    structure: "Stock",
    structure_type: "Stock",
    risk_profile: "equity",
    expiry: "",
    contracts: 300,
    direction: "LONG",
    entry_cost: 96000,
    max_risk: null,
    market_value: 105000,
    legs: [
      {
        direction: "LONG",
        contracts: 300,
        type: "Stock",
        strike: null,
        entry_cost: 96000,
        avg_cost: 320,
        market_price: 350,
        market_value: 105000,
        market_price_is_calculated: false,
      },
    ],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "",
    ...overrides,
  };
}

describe("buildCloseTicket — stock", () => {
  const prices: Record<string, PriceData> = {
    TSLA: { last: 350, bid: 349.9, ask: 350.1, close: 345 } as PriceData,
  };

  it("LONG stock → SELL full qty at last price", () => {
    const draft = buildCloseTicket(
      stockPos({ direction: "LONG", contracts: 300 }),
      prices,
    );
    expect(draft.payload.type).toBe("stock");
    expect(draft.payload.action).toBe("SELL");
    expect(draft.payload.quantity).toBe(300);
    expect(draft.payload.symbol).toBe("TSLA");
    expect(draft.payload.limitPrice).toBe(350);
    expect(draft.payload.tif).toBe("DAY");
  });

  it("SHORT stock → BUY full qty", () => {
    const draft = buildCloseTicket(
      stockPos({ direction: "SHORT", contracts: 300 }),
      prices,
    );
    expect(draft.payload.action).toBe("BUY");
    expect(draft.payload.quantity).toBe(300);
  });

  it("uses bid/ask mid when last is missing", () => {
    const draft = buildCloseTicket(stockPos(), {
      TSLA: { last: null, bid: 349, ask: 351, close: 345 } as PriceData,
    });
    expect(draft.payload.limitPrice).toBe(350);
  });
});
```

- [ ] **Step 2: Run tests — expect failure**

Run: `cd web && npm test -- position-order-close-preset.test.ts`
Expected: FAIL (module not found `@/lib/positionOrderPresets`).

- [ ] **Step 3: Create `positionOrderPresets.ts` with stock-only implementation**

Create `web/lib/positionOrderPresets.ts`:

```ts
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

/**
 * Payload shape matches POST /api/orders/place. Mirrors the shapes produced
 * by `buildSingleLegOrderPayload` and the combo form in OrderTab.tsx.
 */
export type ClosePayload =
  | {
      type: "stock";
      symbol: string;
      action: "BUY" | "SELL";
      quantity: number;
      limitPrice: number;
      tif: "DAY" | "GTC";
    }
  | {
      type: "option";
      symbol: string;
      action: "BUY" | "SELL";
      quantity: number;
      limitPrice: number;
      tif: "DAY" | "GTC";
      expiry: string; // YYYYMMDD
      strike: number;
      right: "C" | "P";
    }
  | {
      type: "combo";
      symbol: string;
      action: "BUY" | "SELL";
      quantity: number;
      limitPrice: number;
      tif: "DAY" | "GTC";
      legs: Array<{
        expiry: string; // YYYYMMDD
        strike: number;
        right: "C" | "P";
        action: "BUY" | "SELL";
        ratio: number;
      }>;
    };

export type CloseTicketDraft = {
  payload: ClosePayload;
  /** Midpoint reference for UI display (may differ from payload.limitPrice after edits). */
  referenceMid: number | null;
};

function midFromQuote(p: PriceData | undefined | null): number | null {
  if (!p) return null;
  if (p.last != null && Number.isFinite(p.last) && p.last > 0) return p.last;
  if (p.bid != null && p.ask != null && p.bid > 0 && p.ask > 0) {
    return (p.bid + p.ask) / 2;
  }
  return null;
}

export function buildCloseTicket(
  position: PortfolioPosition,
  prices: Record<string, PriceData>,
): CloseTicketDraft {
  const isStock = position.structure_type === "Stock";

  if (isStock) {
    const action: "BUY" | "SELL" =
      position.direction === "LONG" ? "SELL" : "BUY";
    const mid = midFromQuote(prices[position.ticker]);
    const limitPrice = mid ?? 0;
    return {
      payload: {
        type: "stock",
        symbol: position.ticker,
        action,
        quantity: Math.abs(position.contracts),
        limitPrice,
        tif: "DAY",
      },
      referenceMid: mid,
    };
  }

  throw new Error("Non-stock close tickets not yet implemented");
}
```

- [ ] **Step 4: Run tests — expect pass**

Run: `cd web && npm test -- position-order-close-preset.test.ts`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/positionOrderPresets.ts web/tests/position-order-close-preset.test.ts
git commit -m "feat(orders): buildCloseTicket for stock positions"
```

---

## Task 2: `buildCloseTicket` for single-leg options

**Files:**

- Modify: `web/lib/positionOrderPresets.ts`
- Test: `web/tests/position-order-close-preset.test.ts`

- [ ] **Step 1: Add failing tests for single-leg options**

Append to `web/tests/position-order-close-preset.test.ts`:

```ts
import { optionKey } from "@/lib/pricesProtocol";

function singleLegOptionPos(overrides: {
  direction: "LONG" | "SHORT";
  type: "Call" | "Put";
  strike: number;
  expiry: string; // YYYY-MM-DD
  contracts: number;
}): PortfolioPosition {
  const { direction, type, strike, expiry, contracts } = overrides;
  return {
    id: 2,
    ticker: "AAPL",
    structure: type === "Call" ? "Long Call" : "Long Put",
    structure_type: type === "Call" ? "LongCall" : "LongPut",
    risk_profile: "defined",
    expiry,
    contracts,
    direction,
    entry_cost: 500,
    max_risk: 500,
    market_value: 600,
    legs: [
      {
        direction,
        contracts,
        type,
        strike,
        entry_cost: 500,
        avg_cost: 5,
        market_price: 6,
        market_value: 600,
        market_price_is_calculated: false,
      },
    ],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "",
  };
}

describe("buildCloseTicket — single-leg option", () => {
  const expiry = "2026-06-19";
  const pos = singleLegOptionPos({
    direction: "LONG",
    type: "Call",
    strike: 200,
    expiry,
    contracts: 5,
  });
  const key = optionKey("AAPL", expiry.replace(/-/g, ""), 200, "C");
  const prices: Record<string, PriceData> = {
    [key]: { last: 6, bid: 5.9, ask: 6.1, close: 5 } as PriceData,
  };

  it("LONG call → SELL-to-close with option payload fields", () => {
    const draft = buildCloseTicket(pos, prices);
    expect(draft.payload.type).toBe("option");
    expect(draft.payload.action).toBe("SELL");
    expect(draft.payload.quantity).toBe(5);
    if (draft.payload.type === "option") {
      expect(draft.payload.strike).toBe(200);
      expect(draft.payload.right).toBe("C");
      expect(draft.payload.expiry).toBe("20260619");
      expect(draft.payload.limitPrice).toBe(6);
    }
  });

  it("SHORT put → BUY-to-close", () => {
    const shortPut = singleLegOptionPos({
      direction: "SHORT",
      type: "Put",
      strike: 180,
      expiry,
      contracts: 2,
    });
    const k = optionKey("AAPL", "20260619", 180, "P");
    const draft = buildCloseTicket(shortPut, {
      [k]: { last: 3, bid: 2.9, ask: 3.1, close: 3.2 } as PriceData,
    });
    expect(draft.payload.action).toBe("BUY");
    expect(draft.payload.quantity).toBe(2);
    if (draft.payload.type === "option") {
      expect(draft.payload.right).toBe("P");
      expect(draft.payload.strike).toBe(180);
    }
  });
});
```

- [ ] **Step 2: Run tests — expect failure**

Run: `cd web && npm test -- position-order-close-preset.test.ts`
Expected: 2 new tests FAIL ("Non-stock close tickets not yet implemented").

- [ ] **Step 3: Implement single-leg option branch**

Replace the `throw` in `buildCloseTicket` with:

```ts
const isSingleLegOption =
  position.legs.length === 1 && position.legs[0].strike != null;

if (isSingleLegOption) {
  const leg = position.legs[0];
  const right: "C" | "P" = leg.type === "Call" ? "C" : "P";
  const expiry = position.expiry.replace(/-/g, "");
  const action: "BUY" | "SELL" = position.direction === "LONG" ? "SELL" : "BUY";
  // Price lookup uses the option-level WS key.
  const key = `${position.ticker}:${expiry}:${leg.strike}:${right}`;
  const mid = midFromQuote(prices[key]);
  return {
    payload: {
      type: "option",
      symbol: position.ticker,
      action,
      quantity: Math.abs(position.contracts),
      limitPrice: mid ?? 0,
      tif: "DAY",
      expiry,
      strike: leg.strike!,
      right,
    },
    referenceMid: mid,
  };
}

throw new Error("Combo close tickets not yet implemented");
```

Add import at top:

```ts
import { optionKey } from "@/lib/pricesProtocol";
```

Replace the inline key string with `optionKey(position.ticker, expiry, leg.strike!, right)` to stay consistent with the rest of the codebase.

- [ ] **Step 4: Run tests — expect pass**

Run: `cd web && npm test -- position-order-close-preset.test.ts`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/positionOrderPresets.ts web/tests/position-order-close-preset.test.ts
git commit -m "feat(orders): buildCloseTicket for single-leg options"
```

---

## Task 3: `buildCloseTicket` for combo positions

**Files:**

- Modify: `web/lib/positionOrderPresets.ts`
- Test: `web/tests/position-order-close-preset.test.ts`

- [ ] **Step 1: Add failing combo tests**

Append to `web/tests/position-order-close-preset.test.ts`:

```ts
import { legPriceKey } from "@/lib/positionUtils";

function bullCallSpreadPos(): PortfolioPosition {
  // LONG $200C, SHORT $210C — net LONG (debit paid)
  return {
    id: 3,
    ticker: "SPY",
    structure: "Bull Call Spread",
    structure_type: "BullCallSpread",
    risk_profile: "defined",
    expiry: "2026-06-19",
    contracts: 4,
    direction: "LONG",
    entry_cost: 1200,
    max_risk: 1200,
    market_value: 1400,
    legs: [
      {
        direction: "LONG",
        contracts: 4,
        type: "Call",
        strike: 200,
        entry_cost: 1800,
        avg_cost: 4.5,
        market_price: 5,
        market_value: 2000,
        market_price_is_calculated: false,
      },
      {
        direction: "SHORT",
        contracts: 4,
        type: "Call",
        strike: 210,
        entry_cost: -600,
        avg_cost: 1.5,
        market_price: 1.5,
        market_value: -600,
        market_price_is_calculated: false,
      },
    ],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "",
  };
}

describe("buildCloseTicket — combo (bull call spread)", () => {
  const pos = bullCallSpreadPos();
  const expiry = "20260619";
  const prices: Record<string, PriceData> = {
    [legPriceKey("SPY", expiry, pos.legs[0])!]: {
      last: 5,
      bid: 4.9,
      ask: 5.1,
      close: 4.5,
    } as PriceData,
    [legPriceKey("SPY", expiry, pos.legs[1])!]: {
      last: 1.5,
      bid: 1.4,
      ask: 1.6,
      close: 1.5,
    } as PriceData,
  };

  it("produces combo payload with Order.action = SELL (closing LONG structure)", () => {
    const draft = buildCloseTicket(pos, prices);
    expect(draft.payload.type).toBe("combo");
    expect(draft.payload.action).toBe("SELL");
    expect(draft.payload.quantity).toBe(4);
  });

  it("per-leg ComboLeg.action stays LONG=BUY, SHORT=SELL regardless of Order.action", () => {
    // This is the load-bearing regression guard: flipping this causes IB error 201
    // (double-reversal). See web/CLAUDE.md → "IB Combo (BAG) Order Leg Convention".
    const draft = buildCloseTicket(pos, prices);
    if (draft.payload.type === "combo") {
      expect(draft.payload.legs).toHaveLength(2);
      const longLeg = draft.payload.legs.find((l) => l.strike === 200)!;
      const shortLeg = draft.payload.legs.find((l) => l.strike === 210)!;
      expect(longLeg.action).toBe("BUY"); // LONG leg → BUY
      expect(shortLeg.action).toBe("SELL"); // SHORT leg → SELL
      expect(longLeg.right).toBe("C");
      expect(shortLeg.right).toBe("C");
      expect(longLeg.ratio).toBe(1);
      expect(shortLeg.ratio).toBe(1);
    }
  });

  it("net direction derived from leg signs, not P&L", () => {
    // Invert: SHORT the spread (credit spread). Order.action should become BUY to close.
    const shortSpread: PortfolioPosition = {
      ...pos,
      direction: "SHORT",
      legs: [
        { ...pos.legs[0], direction: "SHORT" },
        { ...pos.legs[1], direction: "LONG" },
      ],
    };
    const draft = buildCloseTicket(shortSpread, prices);
    expect(draft.payload.action).toBe("BUY");
    if (draft.payload.type === "combo") {
      const leg200 = draft.payload.legs.find((l) => l.strike === 200)!;
      const leg210 = draft.payload.legs.find((l) => l.strike === 210)!;
      expect(leg200.action).toBe("SELL"); // SHORT leg → SELL
      expect(leg210.action).toBe("BUY"); // LONG leg → BUY
    }
  });
});
```

- [ ] **Step 2: Run tests — expect failure**

Run: `cd web && npm test -- position-order-close-preset.test.ts`
Expected: 3 new tests FAIL ("Combo close tickets not yet implemented").

- [ ] **Step 3: Implement combo branch**

Append to `buildCloseTicket` (before the final throw):

```ts
// Combo (multi-leg)
const expiry = position.expiry.replace(/-/g, "");
const comboLegs = position.legs.map((leg) => {
  const right: "C" | "P" = leg.type === "Call" ? "C" : "P";
  // Per web/CLAUDE.md "IB Combo (BAG) Order Leg Convention":
  // ComboLeg.action = spread structure (LONG → BUY, SHORT → SELL), NOT trade direction.
  const legAction: "BUY" | "SELL" = leg.direction === "LONG" ? "BUY" : "SELL";
  return {
    expiry,
    strike: leg.strike!,
    right,
    action: legAction,
    ratio: 1,
  };
});

// Order.action: reverse of the structure's net direction.
const orderAction: "BUY" | "SELL" =
  position.direction === "LONG" ? "SELL" : "BUY";

// Net mid: sum of sign × leg_mid, where sign comes from the position (LONG=+1, SHORT=-1).
// This matches computeNetOptionQuote's definition — keep in mind net can be negative (credit).
let netMid: number | null = 0;
let missing = false;
for (const leg of position.legs) {
  const right: "C" | "P" = leg.type === "Call" ? "C" : "P";
  const key = optionKey(position.ticker, expiry, leg.strike!, right);
  const legMid = midFromQuote(prices[key]);
  if (legMid == null) {
    missing = true;
    break;
  }
  const sign = leg.direction === "LONG" ? 1 : -1;
  netMid = (netMid as number) + sign * legMid;
}
const referenceMid = missing ? null : (netMid as number);

return {
  payload: {
    type: "combo",
    symbol: position.ticker,
    action: orderAction,
    quantity: Math.abs(position.contracts),
    limitPrice: referenceMid ?? 0,
    tif: "DAY",
    legs: comboLegs,
  },
  referenceMid,
};
```

Remove the trailing `throw new Error("Combo close tickets not yet implemented");`.

- [ ] **Step 4: Run tests — expect pass**

Run: `cd web && npm test -- position-order-close-preset.test.ts`
Expected: 8 PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/positionOrderPresets.ts web/tests/position-order-close-preset.test.ts
git commit -m "feat(orders): buildCloseTicket for combo (BAG) positions"
```

---

## Task 4: Qty chip helper with zero-qty guard

**Files:**

- Modify: `web/lib/positionOrderPresets.ts`
- Test: `web/tests/position-order-close-preset.test.ts`

- [ ] **Step 1: Add failing tests for qty chips**

Append to test file:

```ts
import { applyQtyChip } from "@/lib/positionOrderPresets";

describe("applyQtyChip", () => {
  it("100% returns full qty", () => {
    expect(applyQtyChip(7, 1.0)).toBe(7);
  });
  it("50% rounds half-up", () => {
    expect(applyQtyChip(7, 0.5)).toBe(4);
  });
  it("25% rounds half-up", () => {
    expect(applyQtyChip(7, 0.25)).toBe(2);
  });
  it("clamps zero to 1 (so chip never yields 0)", () => {
    expect(applyQtyChip(1, 0.25)).toBe(1);
    expect(applyQtyChip(2, 0.25)).toBe(1);
  });
  it("handles 0 contracts by returning 0 (nothing to close)", () => {
    expect(applyQtyChip(0, 1.0)).toBe(0);
  });
});
```

- [ ] **Step 2: Run — expect failure**

Run: `cd web && npm test -- position-order-close-preset.test.ts`
Expected: 5 new FAIL ("applyQtyChip is not a function").

- [ ] **Step 3: Implement `applyQtyChip`**

Append to `web/lib/positionOrderPresets.ts`:

```ts
/**
 * Apply a percentage chip to a qty, rounding half-up, with a min-1 clamp
 * when the source qty is non-zero. A 25% chip on 2 contracts would otherwise
 * round to 0, which would submit an empty order.
 */
export function applyQtyChip(fullQty: number, pct: number): number {
  if (fullQty <= 0) return 0;
  const raw = Math.round(fullQty * pct);
  return Math.max(1, raw);
}
```

- [ ] **Step 4: Run — expect pass**

Run: `cd web && npm test -- position-order-close-preset.test.ts`
Expected: 13 PASS total.

- [ ] **Step 5: Commit**

```bash
git add web/lib/positionOrderPresets.ts web/tests/position-order-close-preset.test.ts
git commit -m "feat(orders): applyQtyChip helper with zero-qty guard"
```

---

## Task 5: `PositionOrderModal` skeleton with preset tiles

**Files:**

- Create: `web/components/PositionOrderModal.tsx`
- Create: `web/tests/position-order-modal.test.tsx`

- [ ] **Step 1: Write failing component test for preset tiles**

Create `web/tests/position-order-modal.test.tsx`:

```tsx
/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, cleanup } from "@testing-library/react";
import PositionOrderModal from "@/components/PositionOrderModal";
import type { PortfolioPosition } from "@/lib/types";

vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="modal">{children}</div>
  ),
}));

afterEach(() => cleanup());

const pos: PortfolioPosition = {
  id: 1,
  ticker: "TSLA",
  structure: "Stock",
  structure_type: "Stock",
  risk_profile: "equity",
  expiry: "",
  contracts: 300,
  direction: "LONG",
  entry_cost: 96000,
  max_risk: null,
  market_value: 105000,
  legs: [
    {
      direction: "LONG",
      contracts: 300,
      type: "Stock",
      strike: null,
      entry_cost: 96000,
      avg_cost: 320,
      market_price: 350,
      market_value: 105000,
      market_price_is_calculated: false,
    },
  ],
  ib_daily_pnl: null,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "",
};

describe("PositionOrderModal — preset tiles", () => {
  it("renders four preset tiles: Close active, others disabled", () => {
    const { getByRole } = render(
      <PositionOrderModal
        position={pos}
        prices={{ TSLA: { last: 350 } as any }}
        onClose={() => {}}
      />,
    );
    const close = getByRole("button", { name: /^Close$/ });
    const tsl = getByRole("button", { name: /Trailing Stop Loss/i });
    const ttp = getByRole("button", { name: /Trailing Take Profit/i });
    const roll = getByRole("button", { name: /^Roll$/ });
    expect(close.getAttribute("aria-pressed")).toBe("true");
    expect(tsl.hasAttribute("disabled")).toBe(true);
    expect(ttp.hasAttribute("disabled")).toBe(true);
    expect(roll.hasAttribute("disabled")).toBe(true);
    expect(tsl.getAttribute("title")).toMatch(/coming soon/i);
    expect(roll.getAttribute("title")).toMatch(/coming soon/i);
  });
});
```

- [ ] **Step 2: Run — expect failure**

Run: `cd web && npm test -- position-order-modal.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Create the modal skeleton**

Create `web/components/PositionOrderModal.tsx`:

```tsx
"use client";

import { useState } from "react";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import Modal from "./Modal";

type Preset = "close" | "trailing_sl" | "trailing_tp" | "roll";

const PRESETS: ReadonlyArray<{
  id: Preset;
  label: string;
  disabled: boolean;
  tooltip?: string;
}> = [
  { id: "close", label: "Close", disabled: false },
  {
    id: "trailing_sl",
    label: "Trailing Stop Loss",
    disabled: true,
    tooltip: "Coming soon — requires TRAIL order support",
  },
  {
    id: "trailing_tp",
    label: "Trailing Take Profit",
    disabled: true,
    tooltip: "Coming soon — requires TRAIL order support",
  },
  {
    id: "roll",
    label: "Roll",
    disabled: true,
    tooltip: "Coming soon — restructuring ticket in follow-up spec",
  },
];

type Props = {
  position: PortfolioPosition;
  prices: Record<string, PriceData>;
  onClose: () => void;
  onSubmitted?: (orderId: string) => void;
};

export default function PositionOrderModal({
  position,
  prices,
  onClose,
  onSubmitted,
}: Props) {
  const [active, setActive] = useState<Preset>("close");

  return (
    <Modal
      onClose={onClose}
      title={`Order — ${position.ticker} ${position.structure}`}
    >
      <div
        className="position-order-preset-bar"
        role="group"
        aria-label="Order presets"
      >
        {PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => !p.disabled && setActive(p.id)}
            disabled={p.disabled}
            aria-pressed={active === p.id}
            title={p.tooltip}
            className={`preset-tile ${active === p.id ? "active" : ""}`}
          >
            {p.label}
          </button>
        ))}
      </div>
      {active === "close" && (
        <div data-testid="close-preset-panel">
          {/* Close form — wired in Task 6 */}
        </div>
      )}
    </Modal>
  );
}
```

- [ ] **Step 4: Run — expect pass**

Run: `cd web && npm test -- position-order-modal.test.tsx`
Expected: 1 PASS.

- [ ] **Step 5: Commit**

```bash
git add web/components/PositionOrderModal.tsx web/tests/position-order-modal.test.tsx
git commit -m "feat(orders): PositionOrderModal skeleton with preset tiles"
```

---

## Task 6: Close preset form — qty chips, price, submit

**Files:**

- Modify: `web/components/PositionOrderModal.tsx`
- Modify: `web/tests/position-order-modal.test.tsx`

- [ ] **Step 1: Write failing tests for Close form**

Append to `web/tests/position-order-modal.test.tsx`:

```tsx
import { fireEvent, waitFor } from "@testing-library/react";

describe("PositionOrderModal — Close form", () => {
  it("shows default qty equal to full position.contracts", () => {
    const { getByLabelText } = render(
      <PositionOrderModal
        position={pos}
        prices={{ TSLA: { last: 350 } as any }}
        onClose={() => {}}
      />,
    );
    const qty = getByLabelText(/Quantity/i) as HTMLInputElement;
    expect(qty.value).toBe("300");
  });

  it("50% chip halves qty and shows partial-close note", () => {
    const { getByRole, getByText } = render(
      <PositionOrderModal
        position={pos}
        prices={{ TSLA: { last: 350 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^50%$/ }));
    expect(getByText(/Partial close — 150 of 300/)).toBeTruthy();
  });

  it("submits POST /api/orders/place with close payload and closes modal on 200", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ order_id: "abc123" }),
    });
    (global as any).fetch = fetchMock;

    const onClose = vi.fn();
    const onSubmitted = vi.fn();
    const { getByRole } = render(
      <PositionOrderModal
        position={pos}
        prices={{ TSLA: { last: 350 } as any }}
        onClose={onClose}
        onSubmitted={onSubmitted}
      />,
    );
    fireEvent.click(getByRole("button", { name: /Submit close/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/orders/place");
    const body = JSON.parse((init as any).body);
    expect(body).toMatchObject({
      type: "stock",
      symbol: "TSLA",
      action: "SELL",
      quantity: 300,
      tif: "DAY",
    });
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith("abc123"));
    expect(onClose).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run — expect failure**

Run: `cd web && npm test -- position-order-modal.test.tsx`
Expected: 3 new FAIL (no qty input, no chip buttons, no submit button).

- [ ] **Step 3: Add the Close form body**

Replace the `close-preset-panel` block in `PositionOrderModal.tsx`:

```tsx
{
  active === "close" && (
    <ClosePresetForm
      position={position}
      prices={prices}
      onClose={onClose}
      onSubmitted={onSubmitted}
    />
  );
}
```

Add imports at top of the file:

```tsx
import { useMemo } from "react";
import { buildCloseTicket, applyQtyChip } from "@/lib/positionOrderPresets";
```

Append component at bottom of the file:

```tsx
function ClosePresetForm({
  position,
  prices,
  onClose,
  onSubmitted,
}: {
  position: PortfolioPosition;
  prices: Record<string, PriceData>;
  onClose: () => void;
  onSubmitted?: (orderId: string) => void;
}) {
  const draft = useMemo(
    () => buildCloseTicket(position, prices),
    [position, prices],
  );
  const fullQty = Math.abs(position.contracts);
  const [qty, setQty] = useState<number>(fullQty);
  const [limitPrice, setLimitPrice] = useState<number>(
    draft.payload.limitPrice,
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleChip = (pct: number) => setQty(applyQtyChip(fullQty, pct));

  const handleSubmit = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const body = { ...draft.payload, quantity: qty, limitPrice };
      const res = await fetch("/api/orders/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(
          typeof json.detail === "string"
            ? json.detail
            : "Order placement failed",
        );
        return;
      }
      const orderId = typeof json.order_id === "string" ? json.order_id : "";
      onSubmitted?.(orderId);
      onClose();
    } finally {
      setSubmitting(false);
    }
  };

  const partial = qty < fullQty;

  return (
    <div className="position-order-close-form">
      <div className="chip-row" role="group" aria-label="Quantity preset">
        <button type="button" onClick={() => handleChip(1.0)}>
          100%
        </button>
        <button type="button" onClick={() => handleChip(0.5)}>
          50%
        </button>
        <button type="button" onClick={() => handleChip(0.25)}>
          25%
        </button>
      </div>

      <label>
        Quantity
        <input
          type="number"
          min={1}
          max={fullQty}
          value={qty}
          onChange={(e) =>
            setQty(Math.max(1, parseInt(e.target.value, 10) || 1))
          }
        />
      </label>

      {partial && (
        <p className="partial-close-note">
          Partial close — {qty} of {fullQty} contracts
        </p>
      )}

      <label>
        Limit Price
        <input
          type="number"
          step="0.01"
          value={limitPrice}
          onChange={(e) => setLimitPrice(parseFloat(e.target.value) || 0)}
        />
      </label>

      {error && <p className="order-error">{error}</p>}

      <button
        type="button"
        onClick={handleSubmit}
        disabled={submitting || qty <= 0 || limitPrice <= 0}
        aria-label="Submit close"
      >
        {submitting ? "Submitting…" : "Submit close"}
      </button>
    </div>
  );
}
```

- [ ] **Step 4: Run — expect pass**

Run: `cd web && npm test -- position-order-modal.test.tsx`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add web/components/PositionOrderModal.tsx web/tests/position-order-modal.test.tsx
git commit -m "feat(orders): Close preset form with qty chips and submit"
```

---

## Task 7: Wire ⚡ button into `PositionTable`, gated on `!readonly`

**Files:**

- Modify: `web/components/PositionTable.tsx`
- Modify: `web/tests/position-table-readonly.test.tsx`

- [ ] **Step 1: Extend readonly test to assert button presence/absence**

Append to `web/tests/position-table-readonly.test.tsx`:

```tsx
describe("PositionTable — order button", () => {
  it("renders the order button when readonly=false", () => {
    const { container } = render(<PositionTable positions={[stockPosition]} />);
    expect(container.querySelector("button.position-order-btn")).not.toBeNull();
  });

  it("does NOT render the order button when readonly=true", () => {
    const { container } = render(
      <PositionTable positions={[stockPosition]} readonly={true} />,
    );
    expect(container.querySelector("button.position-order-btn")).toBeNull();
  });
});
```

- [ ] **Step 2: Run — expect failure**

Run: `cd web && npm test -- position-table-readonly.test.tsx`
Expected: 2 new FAIL (no `.position-order-btn`).

- [ ] **Step 3: Add button + modal wiring to `PositionTable.tsx`**

Edit `web/components/PositionTable.tsx`. Add import:

```tsx
import { Zap } from "lucide-react";
import PositionOrderModal from "./PositionOrderModal";
```

In the top-level `PositionTable` component (around line 604), add state:

```tsx
const [activeOrderPosition, setActiveOrderPosition] =
  useState<PortfolioPosition | null>(null);
```

Pass a new prop `onOrderClick` to each `PositionRow`:

```tsx
onOrderClick={readonly ? undefined : (p) => setActiveOrderPosition(p)}
```

Render the modal below the existing `InstrumentDetailModal` block:

```tsx
{
  !readonly && activeOrderPosition && prices && (
    <PositionOrderModal
      position={activeOrderPosition}
      prices={prices}
      onClose={() => setActiveOrderPosition(null)}
    />
  );
}
```

In `PositionRow` props, add:

```tsx
onOrderClick?: (pos: PortfolioPosition) => void;
```

In the ticker cell JSX (the `ticker-with-chevron` span and the standalone `TickerLink`), add the button immediately after `TickerLink`:

```tsx
{
  !readonly && onOrderClick && (
    <button
      type="button"
      className="position-order-btn"
      aria-label={`Create order for ${pos.ticker} position`}
      onClick={() => onOrderClick(pos)}
    >
      <Zap size={12} />
    </button>
  );
}
```

Apply this in both the multi-leg and single-leg ticker cell branches (so the button appears for stocks and combos alike).

- [ ] **Step 4: Run — expect pass**

Run: `cd web && npm test -- position-table-readonly.test.tsx position-order-modal.test.tsx position-order-close-preset.test.ts`
Expected: all PASS.

- [ ] **Step 5: Run full Vitest suite**

Run: `cd web && npm test`
Expected: no new regressions.

- [ ] **Step 6: Run typecheck**

Run: `cd web && npm run typecheck`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add web/components/PositionTable.tsx web/components/PositionOrderModal.tsx web/tests/position-table-readonly.test.tsx
git commit -m "feat(orders): ⚡ button on IB position rows opens PositionOrderModal"
```

---

## Task 8: Minimal CSS for preset tiles and button

**Files:**

- Modify: `web/app/globals.css` (or the project's shared CSS file — search for `.ticker-link {` and place new rules in the same file)

- [ ] **Step 1: Locate the CSS file**

Run: `cd web && grep -rln "\.ticker-link {" app/ styles/ 2>/dev/null | head -1`
Note the path. Expected: `web/app/globals.css` or similar.

- [ ] **Step 2: Append styles**

Append to that file:

```css
.position-order-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  margin-left: 4px;
  padding: 0;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}

.position-order-btn:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}

.position-order-preset-bar {
  display: flex;
  gap: 4px;
  margin-bottom: 12px;
}

.position-order-preset-bar .preset-tile {
  flex: 1;
  padding: 6px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 12px;
}

.position-order-preset-bar .preset-tile.active {
  background: var(--surface-selected);
  color: var(--text-primary);
  border-color: var(--border-strong);
}

.position-order-preset-bar .preset-tile[disabled] {
  opacity: 0.4;
  cursor: not-allowed;
}

.position-order-close-form .chip-row {
  display: flex;
  gap: 4px;
  margin-bottom: 8px;
}

.position-order-close-form .partial-close-note {
  font-size: 11px;
  color: var(--text-secondary);
  margin: 4px 0;
}
```

Per `brand/CLAUDE.md`: use tokens, not raw hex. Border-radius ≤ 4px. If a token name above doesn't exist in the codebase, run `grep -n "\-\-border-subtle\|\-\-surface-selected" web/app/globals.css` to confirm and substitute with the project's actual token names.

- [ ] **Step 3: Commit**

```bash
git add web/app/globals.css
git commit -m "style(orders): CSS for position-order button and preset tiles"
```

---

## Task 9: E2E verification with chrome-cdp

**Files:** none (manual verification per `web/CLAUDE.md` UI rule)

- [ ] **Step 1: Start dev server**

Run: `cd web && npm run dev`
Wait for `Local: http://localhost:3000/` and the FastAPI health check to pass.

- [ ] **Step 2: Open Portfolio → IB tab**

Using chrome-cdp (or manually in a browser), navigate to `http://localhost:3000/portfolio`. Verify the ⚡ button renders in the ticker cell of every position row on the IB tab.

- [ ] **Step 3: Verify modal opens**

Click ⚡ on a stock position. Verify:

- Modal appears
- Four preset tiles render; Close is highlighted; others are greyed out
- Hover over "Trailing Stop Loss" — tooltip reads "Coming soon — requires TRAIL order support"
- Hover over "Roll" — tooltip reads "Coming soon — restructuring ticket in follow-up spec"

- [ ] **Step 4: Verify qty chips and partial note**

Click `50%` chip. Verify:

- Quantity input updates to half of the original
- "Partial close — N of M contracts" note appears below the qty input

- [ ] **Step 5: Verify submit (test mode)**

With `XENON_API_TEST_MODE=1` set (or via `web/tests/fastapiHarness.ts`), click "Submit close". Verify:

- Modal closes
- A toast or entry appears in the Orders tab confirming the order

- [ ] **Step 6: Verify Futu tab gating**

Switch to the Futu tab (via `AccountTabBar`). Verify that no ⚡ buttons are rendered on any row — this is the Gate 4 / naked-short safety regression check.

- [ ] **Step 7: Verify combo case**

Open ⚡ on an option spread (bull call, iron condor, or similar). Verify:

- Modal shows the structure name in the title
- Close preset is selected
- Default limit price is the net mid of the combo
- Submit produces a combo payload (inspect DevTools Network tab: POST /api/orders/place body has `type: "combo"` with `legs` array)

- [ ] **Step 8: Record findings**

If everything passes, move to Task 10. If any step fails, create a follow-up fix task before proceeding.

---

## Task 10: Final gate — full test suite and PR

**Files:** none

- [ ] **Step 1: Run full Vitest suite**

Run: `cd web && npm test`
Expected: all PASS.

- [ ] **Step 2: Run typecheck**

Run: `cd web && npm run typecheck`
Expected: no errors.

- [ ] **Step 3: Run scoped Python tests (sanity)**

Run: `python3.13 scripts/infra/dev/run_pytest_affected.py`
Expected: PASS (we didn't touch Python, so this is a no-op guard).

- [ ] **Step 4: Review diff**

Run: `git log --oneline master..HEAD` and `git diff master -- web/`
Verify all commits are scoped to web/, no backend changes, no unrelated churn.

- [ ] **Step 5: Open PR**

Run:

```bash
gh pr create --title "feat(orders): position-row order button (IB tab)" --body "$(cat <<'EOF'
## Summary
- Adds ⚡ button on each IB-tab position row that opens PositionOrderModal
- Close preset wired end-to-end (stock + single-leg option + combo)
- Trailing SL / Trailing TP / Roll tiles disabled with "coming soon" tooltip
- Gated on !readonly — not rendered on Futu rows (Gate 4 safety)

## Test plan
- [x] Unit tests for buildCloseTicket (stock, options, combo, qty chips)
- [x] Component tests for modal (preset states, close form, submit)
- [x] Regression: readonly=true hides both ticker button and order button
- [x] E2E chrome-cdp: modal opens, chips work, submit roundtrips to /api/orders/place
- [x] Futu tab shows no order buttons

Spec: docs/superpowers/specs/2026-04-22-position-row-order-button-design.md
EOF
)"
```

---

## Self-Review Notes

**Spec coverage:** Every section of the spec maps to a task. UX (button + modal + preset tiles + close form) → Tasks 5, 6, 7, 8. Architecture (new files, modified files) → Tasks 1-7. Testing (unit, component, E2E) → Tasks 1-4 (unit), 5-7 (component), 9 (E2E). Non-goals (TRAIL, Roll, per-leg) are enforced by disabled tiles in Task 5.

**Placeholder scan:** No TBDs, no "add error handling" without code, no unresolved references. Every step that changes code shows the code.

**Type consistency:** `ClosePayload` / `CloseTicketDraft` / `buildCloseTicket` / `applyQtyChip` — names stay identical from Task 1 through Task 7. `PositionOrderModal` props `{ position, prices, onClose, onSubmitted }` consistent across Tasks 5-7.

**Load-bearing test:** Task 3's "per-leg ComboLeg.action stays LONG=BUY, SHORT=SELL" is the regression guard for IB error 201. Marked inline so an out-of-order reader understands its importance.
