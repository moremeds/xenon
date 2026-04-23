import { describe, it, expect } from "vitest";
import { seedTicketFromPosition } from "@/lib/positionOrderPresets";
import type { PortfolioPosition } from "@/lib/types";

const optionPosition: PortfolioPosition = {
  id: 1,
  ticker: "SPY",
  structure: "Long Call",
  structure_type: "long_call",
  risk_profile: "long_option",
  expiry: "2026-05-16",
  contracts: 1,
  direction: "LONG",
  entry_cost: 250,
  max_risk: null,
  market_value: null,
  legs: [
    {
      conId: 111222,
      direction: "LONG",
      contracts: 1,
      type: "Call",
      strike: 500,
      entry_cost: 250,
      avg_cost: 2.5,
      market_price: null,
      market_value: null,
    },
  ],
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-04-01",
};

const stockPosition: PortfolioPosition = {
  id: 2,
  ticker: "AAPL",
  structure: "Stock",
  structure_type: "Stock",
  risk_profile: "stock",
  expiry: "",
  contracts: 100,
  direction: "LONG",
  entry_cost: 15000,
  max_risk: null,
  market_value: null,
  legs: [
    {
      conId: 265598,
      direction: "LONG",
      contracts: 100,
      type: "Stock",
      strike: null,
      entry_cost: 15000,
      avg_cost: 150,
      market_price: null,
      market_value: null,
    },
  ],
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-04-01",
};

const comboPosition: PortfolioPosition = {
  id: 3,
  ticker: "SPY",
  structure: "Bull Call Spread",
  structure_type: "bull_call_spread",
  risk_profile: "vertical",
  expiry: "2026-05-16",
  contracts: 1,
  direction: "LONG",
  entry_cost: 100,
  max_risk: null,
  market_value: null,
  legs: [
    {
      conId: 333444,
      direction: "LONG",
      contracts: 1,
      type: "Call",
      strike: 500,
      entry_cost: 300,
      avg_cost: 3,
      market_price: null,
      market_value: null,
    },
    {
      conId: 555666,
      direction: "SHORT",
      contracts: 1,
      type: "Call",
      strike: 510,
      entry_cost: -200,
      avg_cost: 2,
      market_price: null,
      market_value: null,
    },
  ],
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-04-01",
};

describe("seedTicketFromPosition — conId threading", () => {
  it("populates conId on option payload", () => {
    const draft = seedTicketFromPosition(optionPosition, "close", {});
    expect(draft.payload.type).toBe("option");
    if (draft.payload.type !== "option") throw new Error("narrowing");
    expect(draft.payload.conId).toBe(111222);
  });

  it("populates conId on stock payload", () => {
    const draft = seedTicketFromPosition(stockPosition, "close", {});
    expect(draft.payload.type).toBe("stock");
    if (draft.payload.type !== "stock") throw new Error("narrowing");
    expect(draft.payload.conId).toBe(265598);
  });

  it("populates conId on each combo leg", () => {
    const draft = seedTicketFromPosition(comboPosition, "close", {});
    expect(draft.payload.type).toBe("combo");
    if (draft.payload.type !== "combo") throw new Error("narrowing");
    expect(draft.payload.legs.map((l) => l.conId)).toEqual([333444, 555666]);
  });

  it("falls back to null when leg has no conId", () => {
    const noConId: PortfolioPosition = {
      ...optionPosition,
      legs: [{ ...optionPosition.legs[0], conId: null }],
    };
    const draft = seedTicketFromPosition(noConId, "close", {});
    if (draft.payload.type !== "option") throw new Error("narrowing");
    expect(draft.payload.conId).toBeNull();
  });
});
