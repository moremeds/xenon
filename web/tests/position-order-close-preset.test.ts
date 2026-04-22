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
