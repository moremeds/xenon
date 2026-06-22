/**
 * buildTickerGroups sorts ticker cards by |market value| DESC. That comparison
 * must be in USD — otherwise a ¥/₩ card floats above a larger $ card purely
 * because its native magnitude is numerically bigger.
 *
 * Real 2026-06-22 IB snapshot: 5016 (TSEJ/JPY) MV ¥526,700 ≈ $3,257 should
 * sort BELOW MSFT (USD) MV $40,000 — not above it on the raw ¥ magnitude.
 */
import { describe, it, expect } from "vitest";
import { buildTickerGroups } from "@/lib/portfolioByStructure";
import type { PortfolioPosition, PortfolioLeg } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

const mkPrice = (o: Partial<PriceData> = {}): PriceData =>
  ({
    last: null,
    bid: null,
    ask: null,
    close: null,
    volume: null,
    ...o,
  }) as PriceData;

let idc = 5000;
function mkStock(
  ticker: string,
  contracts: number,
  marketValue: number,
  currency?: string,
): PortfolioPosition {
  const leg: PortfolioLeg = {
    type: "Stock",
    direction: "LONG",
    strike: null,
    contracts,
    avg_cost: 0,
    entry_cost: 0,
    market_price: null,
    market_value: marketValue,
    ...(currency ? { currency } : {}),
  };
  return {
    id: ++idc,
    ticker,
    structure: `Stock (${contracts} shares)`,
    structure_type: "Stock",
    risk_profile: "equity",
    direction: "LONG",
    contracts,
    expiry: "N/A",
    entry_cost: 0,
    market_value: marketValue,
    legs: [leg],
    ...(currency ? { currency } : {}),
  } as PortfolioPosition;
}

const positions = [
  mkStock("5016", 100, 526_700, "JPY"), // ¥526,700 ≈ $3,257
  mkStock("MSFT", 100, 40_000), // $40,000
];
const prices: Record<string, PriceData> = {
  "5016": mkPrice({ last: 5_267 }),
  MSFT: mkPrice({ last: 400 }),
};

describe("buildTickerGroups — cross-ticker sort in USD", () => {
  it("orders by USD market value when an FX rate is supplied", () => {
    const groups = buildTickerGroups(positions, prices, {
      usdPerUnit: { USD: 1, JPY: 0.0061845 },
    });
    // MSFT ($40,000) > 5016 ($3,257) — native ¥526,700 must not float to top.
    expect(groups.map((g) => g.ticker)).toEqual(["MSFT", "5016"]);
  });

  it("falls back to native magnitude ordering without a rate (documents old behavior)", () => {
    const groups = buildTickerGroups(positions, prices, {
      usdPerUnit: { USD: 1 },
    });
    expect(groups.map((g) => g.ticker)).toEqual(["5016", "MSFT"]);
  });

  it("reports group.currency for a non-stock/Unknown foreign position (no USD default)", () => {
    // A Futu JP.* row classifies as Unknown — it has NO stock leg, so deriving
    // the card currency from stock?.currency would default it to USD and leak
    // the ¥ magnitude. group.currency must come from the position itself.
    const unknown: PortfolioPosition = {
      id: 9001,
      ticker: "JP.6981",
      currency: "JPY",
      structure: "Unknown",
      structure_type: "Unknown",
      risk_profile: "complex",
      direction: "LONG",
      contracts: 200,
      expiry: "",
      entry_cost: 2_372_000,
      market_value: 2_450_000,
      legs: [
        {
          type: "Stock",
          direction: "LONG",
          strike: null,
          contracts: 200,
          currency: "JPY",
          avg_cost: 11_860,
          entry_cost: 2_372_000,
          market_price: 12_250,
          market_value: 2_450_000,
        },
      ],
    } as PortfolioPosition;
    const groups = buildTickerGroups([unknown], { "JP.6981": mkPrice() });
    const g = groups.find((x) => x.ticker === "JP.6981");
    expect(g).toBeDefined();
    expect(g!.stock).toBeNull(); // not classified as a stock leg
    expect(g!.currency).toBe("JPY"); // currency still recovered from the position
  });
});
