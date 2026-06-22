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
});
