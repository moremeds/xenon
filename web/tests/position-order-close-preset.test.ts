import { describe, it, expect } from "vitest";
import { buildCloseTicket } from "@/lib/positionOrderPresets";
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
  const key = legPriceKey("AAPL", expiry, pos.legs[0])!;
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
    const k = legPriceKey("AAPL", expiry, shortPut.legs[0])!;
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
