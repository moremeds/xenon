import { describe, it, expect } from "vitest";
import {
  seedTicketFromPosition,
  applyQtyChip,
} from "@/lib/positionOrderPresets";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { legPriceKey } from "@/lib/positionUtils";

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

function singleLegOptionPos(overrides: {
  direction: "LONG" | "SHORT";
  type: "Call" | "Put";
  strike: number;
  expiry: string;
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

function bullCallSpreadPos(): PortfolioPosition {
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
    expect(draft.payload.quantity).toBe(300);
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
    expect(draft.referenceBid).toBeCloseTo(2.3, 2);
    expect(draft.referenceAsk).toBeCloseTo(2.7, 2);
    expect(draft.referenceMid).toBeCloseTo(2.5, 2);
    expect(draft.referenceBid).not.toBeCloseTo(draft.referenceAsk!, 2);
  });

  it("credit spread close: positive limit (BUY-to-close at debit)", () => {
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
    expect(draft.payload.action).toBe("BUY");
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
