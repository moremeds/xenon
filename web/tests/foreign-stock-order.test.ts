import { describe, it, expect } from "vitest";
import { seedTicketFromPosition } from "@/lib/positionOrderPresets";
import type { PortfolioPosition } from "@/lib/types";

// Real IB positions (2026-06-22): 5016 TSEJ/JPY, AAPL SMART/USD.
function stockPos(over: Partial<PortfolioPosition>): PortfolioPosition {
  return {
    id: 1,
    ticker: "5016",
    structure: "Stock (100 shares)",
    structure_type: "Stock",
    risk_profile: "equity",
    expiry: "N/A",
    contracts: 100,
    direction: "LONG",
    entry_cost: 474_700,
    max_risk: null,
    market_value: 526_700,
    legs: [
      {
        direction: "LONG",
        contracts: 100,
        type: "Stock",
        strike: null,
        entry_cost: 474_700,
        avg_cost: 4_747,
        market_price: 5_267,
        market_value: 526_700,
      },
    ],
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "unknown",
    ...over,
  };
}

describe("seedTicketFromPosition — foreign stock venue/currency", () => {
  it("includes exchange + currency for a JPY (TSEJ) position", () => {
    const draft = seedTicketFromPosition(
      stockPos({ currency: "JPY", exchange: "TSEJ" }),
      "add",
      {},
    );
    expect(draft.payload.type).toBe("stock");
    if (draft.payload.type === "stock") {
      expect(draft.payload.exchange).toBe("TSEJ");
      expect(draft.payload.currency).toBe("JPY");
    }
  });

  it("omits exchange/currency for a US (USD) position", () => {
    const draft = seedTicketFromPosition(
      stockPos({ ticker: "AAPL", currency: "USD", exchange: "SMART" }),
      "add",
      {},
    );
    if (draft.payload.type === "stock") {
      expect(draft.payload.exchange).toBeUndefined();
      expect(draft.payload.currency).toBeUndefined();
    }
  });
});
