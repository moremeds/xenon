# Position Order Modal — Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework `PositionOrderModal` so it (a) mirrors the layout/primitives of `ModifyOrderModal`, (b) supports both Close and Add (scale-in) actions from the same entry point, and (c) fixes the consensus bugs surfaced in the PR #33 tribunal review (natural-market combo pricing, credit-spread block, string-state inputs, attempt-id refresh, `quote_token`).

**Architecture:** Replace the preset-tile bar with a Close/Add segmented control plus the canonical two-pane layout used by `ModifyOrderModal` (`modify-primary-panel` + optional `modify-secondary-panel`). Reuse the shared primitives from `@/lib/order` (`OrderPriceStrip`, `OrderLegPills`, `OrderPriceButtons`, `OrderQuantityInput`, `OrderPriceInput`) and from `@/components/QuoteTelemetry` (`ModifyOrderQuoteTelemetry`). Replace `buildCloseTicket` with `seedTicketFromPosition(position, intent, prices)` that emits a payload for either direction and computes combo `netBid`/`netAsk`/`netLast` via natural-market cross-fields. Trailing/Roll presets are dropped from this rework — they are explicit follow-ups.

**Tech Stack:** Next.js (App Router) client components, React 19 hooks, Vitest + jsdom for unit tests, Playwright for E2E. CSS reuses `modify-*` classes already in `web/app/globals.css`.

**Out of scope (follow-ups):** Trailing SL/TP, Roll, Covered-call/Collar/Synthetic combo close, editable combo legs (we render leg pills read-only this round), `quote_token` integration (requires `con_id` resolution per leg — separate task; v1 proceeds without it since the route accepts missing token).

---

## File Structure

| Path                                            | Status                                                   | Responsibility                                                                                                                                                                                                                                                          |
| ----------------------------------------------- | -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `web/lib/positionOrderPresets.ts`               | Modify (rename internal API)                             | Rename `buildCloseTicket` → `seedTicketFromPosition(position, intent, prices)`. Keep `applyQtyChip`. Compute combo `netBid`/`netAsk`/`netLast` via natural-market cross-fields. Return `{ payload, referenceMid, referenceBid, referenceAsk }`.                         |
| `web/components/PositionOrderModal.tsx`         | Rewrite                                                  | Two-pane modal mirroring `ModifyOrderModal`. Close/Add segmented control, BID/MID/ASK quick buttons, leg pills (read-only), string-state qty + price inputs, RTH toggle (stock + single-leg only), attempt-id `onFieldEdit` wired up, accepts negative limit for combo. |
| `web/components/PositionTable.tsx`              | Modify (1 line)                                          | Continue passing `prices`; no API change.                                                                                                                                                                                                                               |
| `web/app/globals.css`                           | Modify                                                   | Drop `.position-order-preset-bar`, `.preset-tile`, `.position-order-close-form`, `.partial-close-note`. Keep `.position-order-btn` and `.ticker-with-chevron`. The new modal reuses existing `.modify-*` classes.                                                       |
| `web/tests/position-order-close-preset.test.ts` | Rewrite (rename to `position-order-seed-ticket.test.ts`) | Cover Close + Add for stock / single-leg / combo, natural-market combo pricing, credit-spread (negative limit), fractional shares, and `applyQtyChip` (unchanged).                                                                                                      |
| `web/tests/position-order-modal.test.tsx`       | Rewrite                                                  | Cover Close/Add toggle, BID/MID/ASK button click sets price, partial-close note, attempt-id rolls on field edit after submit failure, fractional stock qty accepted, negative limit accepted for combo.                                                                 |
| `web/e2e/position-order-button.spec.ts`         | Modify                                                   | Add Close/Add toggle assertion + Add-flow payload assertion.                                                                                                                                                                                                            |

---

## Task 1: Rewrite `seedTicketFromPosition` with intent + natural-market combo pricing

**Files:**

- Modify: `web/lib/positionOrderPresets.ts` (full rewrite of `buildCloseTicket`; add `seedTicketFromPosition`; keep `applyQtyChip`)
- Test: `web/tests/position-order-seed-ticket.test.ts` (renamed from `position-order-close-preset.test.ts`)

- [ ] **Step 1: Write failing tests for the new API**

Create `web/tests/position-order-seed-ticket.test.ts`. Use the existing fixtures from `position-order-close-preset.test.ts` as a starting point (rename the file, then expand). Required test groups:

```ts
import { describe, it, expect } from "vitest";
import {
  seedTicketFromPosition,
  applyQtyChip,
} from "@/lib/positionOrderPresets";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { legPriceKey } from "@/lib/positionUtils";

// (keep stockPos / singleLegOptionPos / bullCallSpreadPos helpers from old file)

describe("seedTicketFromPosition — close intent", () => {
  it("LONG stock + close → SELL full qty at last", () => {
    const draft = seedTicketFromPosition(
      stockPos({ direction: "LONG", contracts: 300 }),
      "close",
      { TSLA: { last: 350, bid: 349.9, ask: 350.1 } as PriceData },
    );
    expect(draft.payload.action).toBe("SELL");
    expect(draft.payload.quantity).toBe(300);
    expect(draft.payload.limitPrice).toBe(350);
    expect(draft.referenceBid).toBe(349.9);
    expect(draft.referenceAsk).toBe(350.1);
  });

  it("SHORT stock + close → BUY full qty", () => {
    const draft = seedTicketFromPosition(
      stockPos({ direction: "SHORT", contracts: 300 }),
      "close",
      { TSLA: { last: 350, bid: 349.9, ask: 350.1 } as PriceData },
    );
    expect(draft.payload.action).toBe("BUY");
  });
});

describe("seedTicketFromPosition — add intent", () => {
  it("LONG stock + add → BUY (same direction as the existing position)", () => {
    const draft = seedTicketFromPosition(
      stockPos({ direction: "LONG", contracts: 300 }),
      "add",
      { TSLA: { last: 350, bid: 349.9, ask: 350.1 } as PriceData },
    );
    expect(draft.payload.action).toBe("BUY");
    expect(draft.payload.quantity).toBe(300); // default seed = full qty; user can edit
  });

  it("SHORT stock + add → SELL (sell more shares to grow the short)", () => {
    const draft = seedTicketFromPosition(
      stockPos({ direction: "SHORT", contracts: 300 }),
      "add",
      { TSLA: { last: 350, bid: 349.9, ask: 350.1 } as PriceData },
    );
    expect(draft.payload.action).toBe("SELL");
  });

  it("LONG single-leg call + add → BUY-to-open more contracts", () => {
    const pos = singleLegOptionPos({
      direction: "LONG",
      type: "Call",
      strike: 200,
      expiry: "2026-06-19",
      contracts: 5,
    });
    const key = legPriceKey("AAPL", "2026-06-19", pos.legs[0])!;
    const draft = seedTicketFromPosition(pos, "add", {
      [key]: { last: 6, bid: 5.9, ask: 6.1 } as PriceData,
    });
    expect(draft.payload.action).toBe("BUY");
    if (draft.payload.type === "option") expect(draft.payload.right).toBe("C");
  });

  it("LONG bull call spread + add → BUY combo (debit, positive limit)", () => {
    const pos = bullCallSpreadPos();
    const expiry = "20260619";
    const prices: Record<string, PriceData> = {
      [legPriceKey("SPY", expiry, pos.legs[0])!]: {
        bid: 4.9,
        ask: 5.1,
        last: 5,
      } as PriceData,
      [legPriceKey("SPY", expiry, pos.legs[1])!]: {
        bid: 1.4,
        ask: 1.6,
        last: 1.5,
      } as PriceData,
    };
    const draft = seedTicketFromPosition(pos, "add", prices);
    expect(draft.payload.action).toBe("BUY");
    // Per-leg ComboLeg.action stays LONG → BUY, SHORT → SELL regardless of order direction.
    if (draft.payload.type === "combo") {
      const longLeg = draft.payload.legs.find((l) => l.strike === 200)!;
      const shortLeg = draft.payload.legs.find((l) => l.strike === 210)!;
      expect(longLeg.action).toBe("BUY");
      expect(shortLeg.action).toBe("SELL");
    }
  });
});

describe("seedTicketFromPosition — natural-market combo pricing", () => {
  it("uses cross-fields for netBid/netAsk (not mid-of-mid)", () => {
    // Bull call spread, asym leg quotes so mid-of-mid would mask the bug.
    const pos = bullCallSpreadPos();
    const expiry = "20260619";
    const prices: Record<string, PriceData> = {
      // LONG $200C: bid 4.50, ask 4.70 (mid 4.60)
      [legPriceKey("SPY", expiry, pos.legs[0])!]: {
        bid: 4.5,
        ask: 4.7,
        last: 4.6,
      } as PriceData,
      // SHORT $210C: bid 2.00, ask 2.20 (mid 2.10)
      [legPriceKey("SPY", expiry, pos.legs[1])!]: {
        bid: 2.0,
        ask: 2.2,
        last: 2.1,
      } as PriceData,
    };
    const draft = seedTicketFromPosition(pos, "close", prices);
    // To CLOSE this LONG spread we SELL the combo:
    //   netBid (proceeds we receive) = bid(LONG leg) − ask(SHORT leg) = 4.50 − 2.20 = 2.30
    //   netAsk (cost if we BUY back) = ask(LONG leg) − bid(SHORT leg) = 4.70 − 2.00 = 2.70
    //   mid = 2.50
    expect(draft.referenceBid).toBeCloseTo(2.3, 2);
    expect(draft.referenceAsk).toBeCloseTo(2.7, 2);
    expect(draft.referenceMid).toBeCloseTo(2.5, 2);
    // Mid-of-mid would have produced bid = ask = 2.50 — the bug we're fixing.
    expect(draft.referenceBid).not.toBeCloseTo(draft.referenceAsk!, 2);
  });

  it("credit spread close: positive limit (BUY-to-close at debit)", () => {
    // Short call spread (we collected credit on open). Close = BUY combo.
    const shortSpread: PortfolioPosition = {
      ...bullCallSpreadPos(),
      direction: "SHORT",
      legs: [
        { ...bullCallSpreadPos().legs[0], direction: "SHORT" },
        { ...bullCallSpreadPos().legs[1], direction: "LONG" },
      ],
    };
    const expiry = "20260619";
    const prices: Record<string, PriceData> = {
      [legPriceKey("SPY", expiry, shortSpread.legs[0])!]: {
        bid: 4.5,
        ask: 4.7,
        last: 4.6,
      } as PriceData,
      [legPriceKey("SPY", expiry, shortSpread.legs[1])!]: {
        bid: 2.0,
        ask: 2.2,
        last: 2.1,
      } as PriceData,
    };
    const draft = seedTicketFromPosition(shortSpread, "close", prices);
    expect(draft.payload.action).toBe("BUY"); // BUY-to-close
    // referenceMid for closing a credit spread should be a positive debit (cost to flatten).
    expect(draft.referenceMid).toBeGreaterThan(0);
  });
});

describe("seedTicketFromPosition — combo natural mid sign matches Order.action", () => {
  it("close LONG spread → SELL combo, payload.limitPrice = referenceMid (positive)", () => {
    const pos = bullCallSpreadPos();
    const expiry = "20260619";
    const prices: Record<string, PriceData> = {
      [legPriceKey("SPY", expiry, pos.legs[0])!]: {
        bid: 4.5,
        ask: 4.7,
        last: 4.6,
      } as PriceData,
      [legPriceKey("SPY", expiry, pos.legs[1])!]: {
        bid: 2.0,
        ask: 2.2,
        last: 2.1,
      } as PriceData,
    };
    const draft = seedTicketFromPosition(pos, "close", prices);
    expect(draft.payload.action).toBe("SELL");
    expect(draft.payload.limitPrice).toBe(draft.referenceMid);
    expect(draft.payload.limitPrice).toBeGreaterThan(0);
  });
});

// Keep the existing rejection test for stock+option combos:
describe("seedTicketFromPosition — guards", () => {
  it("rejects covered call / collar / synthetic", () => {
    const coveredCall: PortfolioPosition = {
      ...bullCallSpreadPos(),
      structure: "Covered Call",
      structure_type: "CoveredCall",
      legs: [
        {
          direction: "LONG",
          contracts: 100,
          type: "Stock",
          strike: null,
          entry_cost: 30000,
          avg_cost: 300,
          market_price: 305,
          market_value: 30500,
          market_price_is_calculated: false,
        },
        bullCallSpreadPos().legs[1],
      ],
    };
    expect(() => seedTicketFromPosition(coveredCall, "close", {})).toThrow(
      /stock\+option/i,
    );
    expect(() => seedTicketFromPosition(coveredCall, "add", {})).toThrow(
      /stock\+option/i,
    );
  });
});

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
  it("clamps zero to 1 when source > 0", () => {
    expect(applyQtyChip(2, 0.25)).toBe(1);
  });
  it("returns 0 when source is 0", () => {
    expect(applyQtyChip(0, 1.0)).toBe(0);
  });
});
```

Then delete the old file:

```bash
git rm web/tests/position-order-close-preset.test.ts
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run tests/position-order-seed-ticket.test.ts`
Expected: all `seedTicketFromPosition` tests FAIL (function not exported), `applyQtyChip` tests PASS.

- [ ] **Step 3: Rewrite `web/lib/positionOrderPresets.ts`**

Replace the entire contents with:

```ts
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { legPriceKey } from "@/lib/positionUtils";

export type Intent = "close" | "add";

export type TicketPayload =
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
      expiry: string;
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
        expiry: string;
        strike: number;
        right: "C" | "P";
        action: "BUY" | "SELL";
        ratio: number;
      }>;
    };

export type TicketDraft = {
  payload: TicketPayload;
  /** Reference values for the UI's BID/MID/ASK quick buttons. May be null when quotes incomplete. */
  referenceBid: number | null;
  referenceMid: number | null;
  referenceAsk: number | null;
};

function pickStockBidAsk(p: PriceData | undefined | null): {
  bid: number | null;
  ask: number | null;
  mid: number | null;
  last: number | null;
} {
  if (!p) return { bid: null, ask: null, mid: null, last: null };
  const bid =
    p.bid != null && Number.isFinite(p.bid) && p.bid >= 0 ? p.bid : null;
  const ask =
    p.ask != null && Number.isFinite(p.ask) && p.ask > 0 ? p.ask : null;
  const last =
    p.last != null && Number.isFinite(p.last) && p.last > 0 ? p.last : null;
  const mid = bid != null && ask != null ? (bid + ask) / 2 : last;
  return { bid, ask, mid, last };
}

function round2(x: number): number {
  return Math.round(x * 100) / 100;
}

export function seedTicketFromPosition(
  position: PortfolioPosition,
  intent: Intent,
  prices: Record<string, PriceData>,
): TicketDraft {
  const sameDirection = intent === "add"; // add = same as position direction; close = opposite
  const isStock = position.structure_type === "Stock";
  const baseContracts = Math.abs(position.contracts);

  if (isStock) {
    const action: "BUY" | "SELL" = sameDirection
      ? position.direction === "LONG"
        ? "BUY"
        : "SELL"
      : position.direction === "LONG"
        ? "SELL"
        : "BUY";
    const q = pickStockBidAsk(prices[position.ticker]);
    const limitPrice = q.last ?? q.mid ?? 0;
    return {
      payload: {
        type: "stock",
        symbol: position.ticker,
        action,
        quantity: baseContracts,
        limitPrice,
        tif: "DAY",
      },
      referenceBid: q.bid,
      referenceMid: q.mid,
      referenceAsk: q.ask,
    };
  }

  const isSingleLegOption =
    position.legs.length === 1 &&
    position.legs[0].type !== "Stock" &&
    position.legs[0].strike != null;
  if (isSingleLegOption) {
    const leg = position.legs[0];
    const right: "C" | "P" = leg.type === "Call" ? "C" : "P";
    const expiry = position.expiry.replace(/-/g, "");
    const action: "BUY" | "SELL" = sameDirection
      ? position.direction === "LONG"
        ? "BUY"
        : "SELL"
      : position.direction === "LONG"
        ? "SELL"
        : "BUY";
    const key = legPriceKey(position.ticker, position.expiry, leg);
    const q = pickStockBidAsk(key ? prices[key] : null);
    return {
      payload: {
        type: "option",
        symbol: position.ticker,
        action,
        quantity: baseContracts,
        limitPrice: q.mid ?? 0,
        tif: "DAY",
        expiry,
        strike: leg.strike!,
        right,
      },
      referenceBid: q.bid,
      referenceMid: q.mid,
      referenceAsk: q.ask,
    };
  }

  // Combo
  const hasStockLeg = position.legs.some((l) => l.type === "Stock");
  if (hasStockLeg) {
    throw new Error(
      "Close/Add tickets for stock+option structures (Covered Call, Collar, Synthetic) are not yet supported",
    );
  }

  const expiry = position.expiry.replace(/-/g, "");
  const comboLegs = position.legs.map((leg) => {
    const right: "C" | "P" = leg.type === "Call" ? "C" : "P";
    // ComboLeg.action = spread structure, NOT trade direction. LONG → BUY, SHORT → SELL.
    const legAction: "BUY" | "SELL" = leg.direction === "LONG" ? "BUY" : "SELL";
    const ratio =
      baseContracts > 0
        ? Math.max(1, Math.round(Math.abs(leg.contracts) / baseContracts))
        : 1;
    return { expiry, strike: leg.strike!, right, action: legAction, ratio };
  });

  // Order.action: for "close" reverse the structure direction; for "add" match it.
  const orderAction: "BUY" | "SELL" = sameDirection
    ? position.direction === "LONG"
      ? "BUY"
      : "SELL"
    : position.direction === "LONG"
      ? "SELL"
      : "BUY";

  // Natural-market combo bid/ask. Always compute the BUY-combo cost and SELL-combo proceeds
  // from the structure's perspective (LONG legs pay ask / receive bid; SHORT legs receive
  // bid / pay ask). Then assign netBid (the smaller, the SELL-combo proceeds) and netAsk
  // (the larger, the BUY-combo cost) so the strip always shows bid < ask.
  let buyComboCost = 0; // cost to BUY the structure at market
  let sellComboProceeds = 0; // proceeds from SELLing the structure at market
  let netLast = 0;
  let missing = false;
  for (const leg of position.legs) {
    const key = legPriceKey(position.ticker, position.expiry, leg);
    const lp = key ? prices[key] : null;
    if (!lp || lp.bid == null || lp.ask == null) {
      missing = true;
      break;
    }
    if (leg.direction === "LONG") {
      buyComboCost += lp.ask; // LONG leg: pay ask to BUY combo
      sellComboProceeds += lp.bid; // LONG leg: receive bid to SELL combo
    } else {
      buyComboCost -= lp.bid; // SHORT leg: receive bid when BUYing combo
      sellComboProceeds -= lp.ask; // SHORT leg: pay ask when SELLing combo
    }
    const sign = leg.direction === "LONG" ? 1 : -1;
    netLast += sign * (lp.last ?? (lp.bid + lp.ask) / 2);
  }

  let referenceBid: number | null = null;
  let referenceAsk: number | null = null;
  let referenceMid: number | null = null;
  if (!missing) {
    const lo = Math.min(sellComboProceeds, buyComboCost);
    const hi = Math.max(sellComboProceeds, buyComboCost);
    referenceBid = round2(lo);
    referenceAsk = round2(hi);
    referenceMid = round2((lo + hi) / 2);
  }

  // Seed limitPrice with the net mid. Combos may legitimately resolve to negative
  // (credit spreads) — the server route accepts non-zero numbers for combos.
  const limitPrice = referenceMid ?? 0;

  return {
    payload: {
      type: "combo",
      symbol: position.ticker,
      action: orderAction,
      quantity: baseContracts,
      limitPrice,
      tif: "DAY",
      legs: comboLegs,
    },
    referenceBid,
    referenceMid,
    referenceAsk,
  };
}

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run tests/position-order-seed-ticket.test.ts`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/positionOrderPresets.ts web/tests/position-order-seed-ticket.test.ts web/tests/position-order-close-preset.test.ts
git commit -m "refactor(orders): seedTicketFromPosition supports add intent + natural-market combo pricing"
```

---

## Task 2: Rewrite `PositionOrderModal` to mirror `ModifyOrderModal`

**Files:**

- Modify (full rewrite): `web/components/PositionOrderModal.tsx`

The new modal: Close/Add toggle at top, two-pane layout (`modify-primary-panel` always; `modify-secondary-panel` with `OrderLegPills` only when combo), `ModifyOrderQuoteTelemetry`, BID/MID/ASK reference buttons (`OrderPriceButtons` or inline matching `ModifyOrderModal`), string-state qty + price inputs, `useClientAttemptId.onFieldEdit` on every input change, allow negative limit when payload type is combo, Submit button label switches between "Submit close" and "Submit add" depending on intent.

- [ ] **Step 1: Write failing modal tests**

Replace `web/tests/position-order-modal.test.tsx` with:

```tsx
/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, cleanup, fireEvent, waitFor } from "@testing-library/react";
import PositionOrderModal from "@/components/PositionOrderModal";
import type { PortfolioPosition } from "@/lib/types";

vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="modal">{children}</div>
  ),
}));

const markSubmitted = vi.fn();
const markTerminal = vi.fn();
const onFieldEdit = vi.fn();
vi.mock("@/components/ticker-detail/useClientAttemptId", () => ({
  useClientAttemptId: () => ({
    id: "test-attempt-id-123",
    markSubmitted,
    markTerminal,
    onFieldEdit,
  }),
}));

afterEach(() => {
  cleanup();
  markSubmitted.mockReset();
  markTerminal.mockReset();
  onFieldEdit.mockReset();
});

const stockPos: PortfolioPosition = {
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

describe("PositionOrderModal — Close/Add toggle", () => {
  it("defaults to Close intent for a LONG stock → action SELL", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({
        ok: true,
        json: async () => ({ orderId: "abc", status: "ok" }),
      });
    (global as any).fetch = fetchMock;
    const { getByRole } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^Submit/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse((fetchMock.mock.calls[0] as any)[1].body);
    expect(body.action).toBe("SELL");
    expect(body.client_attempt_id).toBe("test-attempt-id-123");
  });

  it("switching to Add toggles action to BUY for the same LONG stock", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({
        ok: true,
        json: async () => ({ orderId: "abc", status: "ok" }),
      });
    (global as any).fetch = fetchMock;
    const { getByRole } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^Add$/i }));
    fireEvent.click(getByRole("button", { name: /^Submit/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse((fetchMock.mock.calls[0] as any)[1].body);
    expect(body.action).toBe("BUY");
  });
});

describe("PositionOrderModal — BID / MID / ASK quick buttons", () => {
  it("clicking BID sets limit price to bid", () => {
    const { getByRole, getByLabelText } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.5, ask: 350.5 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^BID/i }));
    const price = getByLabelText(/Limit Price/i) as HTMLInputElement;
    expect(price.value).toBe("349.50");
  });

  it("clicking ASK sets limit price to ask", () => {
    const { getByRole, getByLabelText } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.5, ask: 350.5 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^ASK/i }));
    const price = getByLabelText(/Limit Price/i) as HTMLInputElement;
    expect(price.value).toBe("350.50");
  });
});

describe("PositionOrderModal — input UX", () => {
  it("user can clear the qty input via backspace and retype", () => {
    const { getByLabelText } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );
    const qty = getByLabelText(/Quantity/i) as HTMLInputElement;
    fireEvent.change(qty, { target: { value: "" } });
    expect(qty.value).toBe(""); // empty allowed during typing
    fireEvent.change(qty, { target: { value: "42" } });
    expect(qty.value).toBe("42");
  });

  it("user can type a minus sign in limit price (combo credit spread)", () => {
    const { getByLabelText } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );
    const price = getByLabelText(/Limit Price/i) as HTMLInputElement;
    fireEvent.change(price, { target: { value: "-" } });
    expect(price.value).toBe("-");
  });

  it("editing qty after a submit attempt rolls the client_attempt_id (calls onFieldEdit)", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: false, json: async () => ({ error: "bad" }) });
    (global as any).fetch = fetchMock;
    const { getByRole, getByLabelText } = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }}
        onClose={() => {}}
      />,
    );
    fireEvent.click(getByRole("button", { name: /^Submit/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    fireEvent.change(getByLabelText(/Quantity/i), { target: { value: "100" } });
    expect(onFieldEdit).toHaveBeenCalledWith("quantity");
  });
});

describe("PositionOrderModal — leg pills (combo)", () => {
  const bullCallSpread: PortfolioPosition = {
    id: 2,
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

  it("renders the OrderLegPills strip for a combo position", () => {
    const { container } = render(
      <PositionOrderModal
        position={bullCallSpread}
        prices={{}}
        onClose={() => {}}
      />,
    );
    expect(container.querySelector(".order-leg-pills")).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run tests/position-order-modal.test.tsx`
Expected: most assertions FAIL (toggle/BID button/legs pills not yet wired).

- [ ] **Step 3: Rewrite `web/components/PositionOrderModal.tsx`**

Replace the entire file with:

```tsx
"use client";

import { useEffect, useMemo, useState } from "react";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import Modal from "./Modal";
import { ModifyOrderQuoteTelemetry } from "./QuoteTelemetry";
import { fmtPrice } from "@/lib/positionUtils";
import { OrderLegPills, type OrderLeg as UnifiedOrderLeg } from "@/lib/order";
import { useClientAttemptId } from "@/components/ticker-detail/useClientAttemptId";
import { getReasonToast } from "@/lib/orderReasonCodes";
import {
  seedTicketFromPosition,
  applyQtyChip,
  type Intent,
  type TicketDraft,
} from "@/lib/positionOrderPresets";

type Props = {
  position: PortfolioPosition;
  prices: Record<string, PriceData>;
  onClose: () => void;
  onSubmitted?: (orderId: string) => void;
};

function errorFromResponseBody(
  body: Record<string, unknown> | null | undefined,
  fallback: string,
): string {
  if (body && typeof body === "object") {
    const code = (body as { reason_code?: unknown }).reason_code;
    if (typeof code === "string" && code.length > 0)
      return getReasonToast(code).copy;
    const err = (body as { error?: unknown }).error;
    if (typeof err === "string" && err.length > 0) return err;
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
  }
  return fallback;
}

function unifiedLegsFromPosition(pos: PortfolioPosition): UnifiedOrderLeg[] {
  return pos.legs
    .filter((l) => l.type !== "Stock" && l.strike != null)
    .map((leg, i) => ({
      id: `leg-${i}`,
      action: leg.direction === "LONG" ? "BUY" : "SELL",
      direction: leg.direction,
      strike: leg.strike!,
      type: leg.type === "Call" ? "Call" : "Put",
      expiry: pos.expiry.replace(/-/g, ""),
      quantity: Math.abs(leg.contracts),
    }));
}

export default function PositionOrderModal({
  position,
  prices,
  onClose,
  onSubmitted,
}: Props) {
  const [intent, setIntent] = useState<Intent>("close");

  const draft: TicketDraft = useMemo(
    () => seedTicketFromPosition(position, intent, prices),
    [position, intent, prices],
  );

  const fullQty = Math.abs(position.contracts);
  const isCombo = draft.payload.type === "combo";

  const [qtyText, setQtyText] = useState<string>(String(fullQty));
  const [priceText, setPriceText] = useState<string>(
    Number.isFinite(draft.payload.limitPrice)
      ? draft.payload.limitPrice.toFixed(2)
      : "",
  );
  const [outsideRth, setOutsideRth] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const attemptId = useClientAttemptId({ ticker: position.ticker });

  // Reseed price when intent or seeded mid changes (live WS updates).
  useEffect(() => {
    if (Number.isFinite(draft.payload.limitPrice)) {
      setPriceText(draft.payload.limitPrice.toFixed(2));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intent, draft.referenceMid]);

  const parsedQty =
    qtyText.trim() === ""
      ? NaN
      : position.structure_type === "Stock"
        ? parseFloat(qtyText)
        : parseInt(qtyText, 10);
  const parsedPrice =
    priceText.trim() === "" || priceText.trim() === "-"
      ? NaN
      : parseFloat(priceText);
  const isValidQty = Number.isFinite(parsedQty) && parsedQty > 0;
  const isValidPrice =
    Number.isFinite(parsedPrice) &&
    (isCombo ? parsedPrice !== 0 : parsedPrice > 0);

  const handleChip = (pct: number) => {
    const next = applyQtyChip(fullQty, pct);
    setQtyText(String(next));
    attemptId.onFieldEdit("quantity");
  };

  const handleSubmit = async () => {
    if (submitting || !isValidQty || !isValidPrice) return;
    setSubmitting(true);
    setError(null);
    try {
      // For "close" intent we still clamp qty to [1, fullQty] so a manual over-type
      // cannot flip a close into an opening trade. For "add" intent there is no upper
      // clamp — the server-side naked-short guard remains the source of truth.
      const clampedQty =
        intent === "close"
          ? Math.max(1, Math.min(fullQty, parsedQty))
          : Math.max(1, parsedQty);
      attemptId.markSubmitted();
      const body = {
        ...draft.payload,
        quantity: clampedQty,
        limitPrice: parsedPrice,
        client_attempt_id: attemptId.id,
        ...(outsideRth && !isCombo ? { outsideRth: true } : {}),
      };
      const res = await fetch("/api/orders/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(errorFromResponseBody(json, "Order placement failed"));
        attemptId.markTerminal();
        return;
      }
      const orderId = typeof json.orderId === "string" ? json.orderId : "";
      attemptId.markTerminal();
      onSubmitted?.(orderId);
      onClose();
    } catch {
      setError("Network error placing order");
      attemptId.markTerminal();
    } finally {
      setSubmitting(false);
    }
  };

  const submitLabel = submitting
    ? intent === "close"
      ? "Submitting close…"
      : "Submitting add…"
    : intent === "close"
      ? "Submit close"
      : "Submit add";

  // Build a PriceData-shaped payload for the telemetry strip when we have combo refs.
  const priceData: PriceData | null = useMemo(() => {
    if (draft.referenceBid == null || draft.referenceAsk == null) return null;
    return {
      symbol: position.ticker,
      last: draft.referenceMid ?? null,
      lastIsCalculated: true,
      bid: draft.referenceBid,
      ask: draft.referenceAsk,
      bidSize: null,
      askSize: null,
      volume: null,
      high: null,
      low: null,
      open: null,
      close: null,
      week52High: null,
      week52Low: null,
      avgVolume: null,
      delta: null,
      gamma: null,
      theta: null,
      vega: null,
      impliedVol: null,
      undPrice: null,
      timestamp: new Date().toISOString(),
    };
  }, [
    draft.referenceBid,
    draft.referenceMid,
    draft.referenceAsk,
    position.ticker,
  ]);

  const partial = intent === "close" && isValidQty && parsedQty < fullQty;

  const unifiedLegs = useMemo(
    () => unifiedLegsFromPosition(position),
    [position],
  );

  return (
    <Modal
      open={true}
      onClose={onClose}
      title={`Order — ${position.ticker} ${position.structure}`}
      className={
        isCombo
          ? "modify-order-modal modify-order-modal-combo"
          : "modify-order-modal"
      }
    >
      <div className={`modify-dialog${isCombo ? " modify-dialog-combo" : ""}`}>
        <div className="modify-order-info">
          <strong>{position.ticker}</strong>
          <span
            className={`pill ${position.direction === "LONG" ? "accum" : "distrib"}`}
          >
            {position.direction}
          </span>
          <span>{position.structure}</span>
          <span>{fullQty}x</span>
        </div>

        {/* Close / Add segmented control */}
        <div
          className="position-order-intent-bar"
          role="group"
          aria-label="Order intent"
        >
          <button
            type="button"
            className={`preset-tile ${intent === "close" ? "active" : ""}`}
            aria-pressed={intent === "close"}
            onClick={() => {
              setIntent("close");
              attemptId.onFieldEdit("intent");
            }}
          >
            Close
          </button>
          <button
            type="button"
            className={`preset-tile ${intent === "add" ? "active" : ""}`}
            aria-pressed={intent === "add"}
            onClick={() => {
              setIntent("add");
              attemptId.onFieldEdit("intent");
            }}
          >
            Add
          </button>
        </div>

        <div
          className={`modify-layout${isCombo ? " modify-layout-combo" : ""}`}
        >
          <div className="modify-primary-panel">
            <ModifyOrderQuoteTelemetry priceData={priceData} />

            <div className="modify-price-section">
              <div
                className={`modify-field-grid${isCombo ? " modify-field-grid-combo" : ""}`}
              >
                <label className="modify-field" htmlFor="position-order-qty">
                  <span className="modify-price-label">Quantity</span>
                  <div className="modify-price-input-row">
                    <input
                      id="position-order-qty"
                      className="modify-price-input"
                      type="text"
                      inputMode="decimal"
                      value={qtyText}
                      onChange={(e) => {
                        setQtyText(e.target.value);
                        attemptId.onFieldEdit("quantity");
                      }}
                    />
                  </div>
                </label>

                <label className="modify-field" htmlFor="position-order-price">
                  <span className="modify-price-label">
                    {isCombo ? "Net Limit Price" : "Limit Price"}
                  </span>
                  <div className="modify-price-input-row">
                    <span className="modify-price-prefix">$</span>
                    <input
                      id="position-order-price"
                      className="modify-price-input"
                      type="text"
                      inputMode="decimal"
                      value={priceText}
                      onChange={(e) => {
                        setPriceText(e.target.value);
                        attemptId.onFieldEdit("limitPrice");
                      }}
                      autoFocus
                    />
                  </div>
                </label>
              </div>

              {intent === "close" && (
                <div
                  className="position-order-chip-row"
                  role="group"
                  aria-label="Close size chips"
                >
                  <button
                    type="button"
                    className="btn-quick"
                    onClick={() => handleChip(1.0)}
                  >
                    100%
                  </button>
                  <button
                    type="button"
                    className="btn-quick"
                    onClick={() => handleChip(0.5)}
                  >
                    50%
                  </button>
                  <button
                    type="button"
                    className="btn-quick"
                    onClick={() => handleChip(0.25)}
                  >
                    25%
                  </button>
                </div>
              )}

              <div className="modify-quick-section">
                <span className="modify-price-label">Reference Price</span>
                <div className="modify-quick-buttons">
                  <button
                    className="btn-quick"
                    disabled={draft.referenceBid == null}
                    onClick={() =>
                      draft.referenceBid != null &&
                      (setPriceText(draft.referenceBid.toFixed(2)),
                      attemptId.onFieldEdit("limitPrice"))
                    }
                  >
                    BID
                    {draft.referenceBid != null
                      ? ` ${draft.referenceBid.toFixed(2)}`
                      : ""}
                  </button>
                  <button
                    className="btn-quick"
                    disabled={draft.referenceMid == null}
                    onClick={() =>
                      draft.referenceMid != null &&
                      (setPriceText(draft.referenceMid.toFixed(2)),
                      attemptId.onFieldEdit("limitPrice"))
                    }
                  >
                    MID
                    {draft.referenceMid != null
                      ? ` ${draft.referenceMid.toFixed(2)}`
                      : ""}
                  </button>
                  <button
                    className="btn-quick"
                    disabled={draft.referenceAsk == null}
                    onClick={() =>
                      draft.referenceAsk != null &&
                      (setPriceText(draft.referenceAsk.toFixed(2)),
                      attemptId.onFieldEdit("limitPrice"))
                    }
                  >
                    ASK
                    {draft.referenceAsk != null
                      ? ` ${draft.referenceAsk.toFixed(2)}`
                      : ""}
                  </button>
                </div>
              </div>

              {!isCombo && (
                <label className="modify-rth-toggle">
                  <input
                    type="checkbox"
                    checked={outsideRth}
                    onChange={(e) => setOutsideRth(e.target.checked)}
                  />
                  <span className="modify-rth-label">FILL OUTSIDE RTH</span>
                  <span className="modify-rth-hint">
                    Pre-market &amp; after hours
                  </span>
                </label>
              )}

              {partial && (
                <p className="partial-close-note">
                  Partial close — {parsedQty} of {fullQty} contracts
                </p>
              )}

              {isValidPrice &&
                draft.referenceMid != null &&
                Math.abs(parsedPrice - draft.referenceMid) >= 0.005 && (
                  <div
                    className={`modify-delta ${parsedPrice - draft.referenceMid > 0 ? "positive" : "negative"}`}
                  >
                    {parsedPrice - draft.referenceMid > 0 ? "+" : ""}
                    {fmtPrice(Math.abs(parsedPrice - draft.referenceMid))} from
                    mid {fmtPrice(draft.referenceMid)}
                  </div>
                )}

              {error && <p className="order-error">{error}</p>}
            </div>
          </div>

          {isCombo && unifiedLegs.length > 0 && (
            <div className="modify-secondary-panel">
              <div style={{ marginBottom: "12px" }}>
                <OrderLegPills legs={unifiedLegs} />
              </div>
              <div className="modify-section-heading">
                <span className="modify-price-label">Legs</span>
                <span className="modify-section-hint">
                  Read-only — leg editing comes in a follow-up
                </span>
              </div>
            </div>
          )}
        </div>

        <div className="modify-actions">
          <button
            className="btn-secondary"
            onClick={onClose}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            className="btn-primary"
            onClick={handleSubmit}
            disabled={submitting || !isValidQty || !isValidPrice}
            aria-label={submitLabel}
          >
            {submitLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}
```

- [ ] **Step 4: Run modal tests**

Run: `cd web && npx vitest run tests/position-order-modal.test.tsx`
Expected: all PASS.

- [ ] **Step 5: Run typecheck**

Run: `cd web && npm run typecheck`
Expected: no NEW errors introduced (the pre-existing TS errors flagged in the original PR are not part of this scope; verify the diff doesn't add any new ones).

- [ ] **Step 6: Commit**

```bash
git add web/components/PositionOrderModal.tsx web/tests/position-order-modal.test.tsx
git commit -m "feat(orders): PositionOrderModal mirrors ModifyOrderModal layout, adds Add intent"
```

---

## Task 3: Update CSS — drop dead classes, add intent bar

**Files:**

- Modify: `web/app/globals.css` (lines around 6593-6651)

- [ ] **Step 1: Remove obsolete styles, add intent bar**

Delete these blocks:

- `.position-order-preset-bar { … }`
- `.position-order-preset-bar .preset-tile { … }`
- `.position-order-preset-bar .preset-tile.active { … }`
- `.position-order-preset-bar .preset-tile[disabled] { … }`
- `.position-order-close-form .chip-row { … }`
- `.position-order-close-form .partial-close-note { … }`

Keep `.position-order-btn` and `.position-order-btn:hover` (the ⚡ entry point).

Insert the new intent-bar styles immediately after `.position-order-btn:hover { … }`:

```css
.position-order-intent-bar {
  display: flex;
  gap: 4px;
  margin-bottom: 12px;
}

.position-order-intent-bar .preset-tile {
  flex: 1;
  padding: 6px 8px;
  border: 1px solid var(--border-dim);
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 12px;
}

.position-order-intent-bar .preset-tile.active {
  background: var(--bg-panel-raised);
  color: var(--text-primary);
  border-color: var(--accent-bg);
}

.position-order-chip-row {
  display: flex;
  gap: 4px;
  margin: 8px 0;
}

.partial-close-note {
  font-size: 11px;
  color: var(--text-secondary);
  margin: 4px 0;
}
```

(`.partial-close-note` is preserved at top level since the new modal references it without the `.position-order-close-form` parent.)

- [ ] **Step 2: Visual smoke**

Run: `cd web && npm run dev`, then in another shell use `gstack` to load `http://localhost:3000` (or relevant route showing IB positions table) and trigger the ⚡ button on any position. Verify:

- Modal opens with two-pane layout matching ModifyOrderModal visually (panel borders, spacing, font sizes)
- Close/Add toggle highlights the active option
- BID/MID/ASK buttons render with prices
- For a combo position, leg pills appear in the right-hand panel
- No console errors

- [ ] **Step 3: Commit**

```bash
git add web/app/globals.css
git commit -m "style(orders): drop preset-bar CSS, add intent-bar styles for new modal"
```

---

## Task 4: Update Playwright E2E for Close/Add toggle

**Files:**

- Modify: `web/e2e/position-order-button.spec.ts`

- [ ] **Step 1: Add toggle assertion test**

Append (after the existing default-close assertions):

```ts
test("Close/Add toggle switches the submit button label and the payload action", async ({
  page,
  mockApi,
}) => {
  // (use the same fixtures as the existing close test)
  await page.goto("/portfolio");
  await page.locator('[aria-label*="Create order for TSLA"]').first().click();
  await expect(page.getByRole("button", { name: /^Close$/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(
    page.getByRole("button", { name: /Submit close/i }),
  ).toBeVisible();

  await page.getByRole("button", { name: /^Add$/ }).click();
  await expect(page.getByRole("button", { name: /^Add$/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.getByRole("button", { name: /Submit add/i })).toBeVisible();

  // Submit Add and verify the captured request body action flipped to BUY (LONG stock + add).
  const requestPromise = page.waitForRequest(
    (req) => req.url().endsWith("/api/orders/place") && req.method() === "POST",
  );
  await page.getByRole("button", { name: /Submit add/i }).click();
  const req = await requestPromise;
  const body = JSON.parse(req.postData() ?? "{}");
  expect(body.action).toBe("BUY");
});
```

(Adapt selector + page route to whatever the existing spec uses — copy the existing setup verbatim.)

- [ ] **Step 2: Run Playwright spec**

Run: `cd web && npx playwright test e2e/position-order-button.spec.ts`
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add web/e2e/position-order-button.spec.ts
git commit -m "test(e2e): position-order modal Close/Add toggle"
```

---

## Task 5: Full test sweep + PR update

- [ ] **Step 1: Run full unit suite**

Run: `cd web && npm test -- --run`
Expected: all tests PASS _except_ the pre-existing 5 unrelated failures noted in the PR description. Verify the failure list is unchanged.

- [ ] **Step 2: Run typecheck**

Run: `cd web && npm run typecheck`
Expected: no new errors vs master.

- [ ] **Step 3: Push + update PR description**

```bash
git push origin feat/position-order-button
```

Then update the PR body via `gh pr edit 33 --body-file -` with a brief rework summary linking to this plan and listing the resolved tribunal issues.

- [ ] **Step 4: Commit any final tweaks if tests surface regressions**

If any tests fail unexpectedly, stop and investigate root cause before committing fixes — do not paper over with retries.

---

## Self-Review Checklist

- **Spec coverage:**
  - User asks: layout parity with ModifyOrderModal → Task 2 (full rewrite reusing modify-\* classes + OrderLegPills + ModifyOrderQuoteTelemetry).
  - User ask: support buy-more / add → Tasks 1 + 2 (intent param threaded through seed function + UI toggle).
  - Tribunal CRITICAL #1 (combo natural-market pricing) → Task 1 step 3.
  - Tribunal CRITICAL #2 (negative limit blocks credit close) → Task 2 step 3 (`isValidPrice` allows non-zero for combo).
  - Tribunal HIGH (string-state inputs / can't backspace / can't type minus) → Task 2 step 3 (text-mode inputs).
  - Tribunal HIGH (qty truncates fractional shares) → Task 2 step 3 (`parseFloat` for stock).
  - Tribunal MEDIUM (`onFieldEdit` not called) → Task 2 step 3 (every onChange and chip + intent toggle calls it).
  - Tribunal MEDIUM (live price reseed) → Task 2 step 3 (`useEffect([intent, draft.referenceMid])`).
  - Tribunal MEDIUM (`quote_token`) → explicitly out of scope (called out at top); follow-up.
  - Tribunal MEDIUM (`acknowledge_limit_override`) → out of scope; follow-up.

- **Placeholder scan:** No TBD/TODO. All code blocks are complete. The Playwright fixture detail in Task 4 says "copy the existing setup verbatim" — acceptable because the existing spec already exists in the file being modified; the executor reads it inline.

- **Type consistency:** `seedTicketFromPosition` returns `TicketDraft { payload, referenceBid, referenceMid, referenceAsk }`. `Intent = "close" | "add"`. The modal imports `Intent`, `TicketDraft`, `seedTicketFromPosition`, `applyQtyChip`. `OrderLegPills` legs use `direction` ∈ `"LONG" | "SHORT"` and `type` ∈ `"Call" | "Put"` per `@/lib/order/types`. CSS classes match across `.modify-*` (existing) and `.position-order-intent-bar` / `.position-order-chip-row` / `.partial-close-note` (new/renamed). All tests reference exported names that exist.
