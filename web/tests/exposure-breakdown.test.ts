/**
 * Unit tests: Exposure breakdown — per-leg delta signs.
 *
 * Short legs must display negative rawDelta so the modal shows
 * the correct sign (e.g. -0.08 for a short call, not +0.08).
 */

import { describe, it, expect } from "vitest";
import {
  computeExposureDetailed,
  positionDeltaForHeader,
} from "@/lib/exposureBreakdown";
import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

function makePriceData(overrides: Partial<PriceData> = {}): PriceData {
  return {
    last: null,
    bid: null,
    ask: null,
    close: null,
    volume: null,
    ...overrides,
  };
}

const SPREAD_POSITION: PortfolioData = {
  bankroll: 100_000,
  positions: [
    {
      id: 1,
      ticker: "AAPL",
      structure: "Bull Call Spread $270.0/$290.0",
      structure_type: "Vertical",
      risk_profile: "defined",
      direction: "DEBIT",
      contracts: 100,
      expiry: "2026-04-17",
      market_value: 41_700,
      legs: [
        {
          type: "Call",
          direction: "LONG",
          strike: 270,
          contracts: 100,
          avg_cost: 901,
          market_value: 63_000,
        },
        {
          type: "Call",
          direction: "SHORT",
          strike: 290,
          contracts: 100,
          avg_cost: -500,
          market_value: -21_300,
        },
      ],
    },
  ],
  account_summary: {},
  exposure: {},
  violations: [],
};

describe("Exposure breakdown — short leg delta sign", () => {
  it("short leg rawDelta is negative when IB provides delta", () => {
    const prices: Record<string, PriceData> = {
      AAPL: makePriceData({ last: 260 }),
      // IB deltas: long call delta = 0.36, short call delta = 0.08
      // Key format: TICKER_YYYYMMDD_STRIKE_RIGHT
      AAPL_20260417_270_C: makePriceData({ last: 6.3, delta: 0.36 }),
      AAPL_20260417_290_C: makePriceData({ last: 2.13, delta: 0.08 }),
    };

    const result = computeExposureDetailed(SPREAD_POSITION, prices);
    const row = result.rows[0];
    expect(row.legs).toHaveLength(2);

    // Long leg: rawDelta should be positive
    const longLeg = row.legs.find((l) => l.direction === "LONG")!;
    expect(longLeg.rawDelta).toBeCloseTo(0.36, 4);
    expect(longLeg.legDelta).toBeCloseTo(3600, 0);

    // Short leg: rawDelta must be NEGATIVE
    const shortLeg = row.legs.find((l) => l.direction === "SHORT")!;
    expect(shortLeg.rawDelta).toBeCloseTo(-0.08, 4);
    expect(shortLeg.legDelta).toBeCloseTo(-800, 0);

    // Position-level delta: net of long - short
    expect(row.delta).toBeCloseTo(2800, 0);
  });

  it("short leg rawDelta is negative with approx delta fallback", () => {
    const prices: Record<string, PriceData> = {
      AAPL: makePriceData({ last: 260 }),
      // No IB delta — will use approx
    };

    const result = computeExposureDetailed(SPREAD_POSITION, prices);
    const row = result.rows[0];

    const shortLeg = row.legs.find((l) => l.direction === "SHORT")!;
    // Short call rawDelta must be negative
    expect(shortLeg.rawDelta).toBeLessThan(0);
    expect(shortLeg.legDelta).toBeLessThan(0);

    const longLeg = row.legs.find((l) => l.direction === "LONG")!;
    expect(longLeg.rawDelta).toBeGreaterThan(0);
    expect(longLeg.legDelta).toBeGreaterThan(0);
  });

  it("stock leg rawDelta reflects direction sign", () => {
    const portfolio: PortfolioData = {
      bankroll: 100_000,
      positions: [
        {
          id: 2,
          ticker: "MSFT",
          structure: "Stock (1000 shares)",
          structure_type: "Stock",
          risk_profile: "equity",
          direction: "LONG",
          contracts: 1000,
          expiry: "N/A",
          market_value: 400_000,
          legs: [
            {
              type: "Stock",
              direction: "LONG",
              strike: null,
              contracts: 1000,
              avg_cost: 468_000,
              market_value: 400_000,
            },
          ],
        },
      ],
      account_summary: {},
      exposure: {},
      violations: [],
    };

    const prices: Record<string, PriceData> = {
      MSFT: makePriceData({ last: 400 }),
    };

    const result = computeExposureDetailed(portfolio, prices);
    const stockLeg = result.rows[0].legs[0];
    expect(stockLeg.rawDelta).toBe(1);
    expect(stockLeg.legDelta).toBe(1000);
  });
});

describe("computeExposureDetailed — FX conversion of foreign exposure", () => {
  // Real 2026-06-22 IB snapshot: 5016 JX Advanced Metals (TSEJ/JPY) 100 sh @
  // ¥5,267; USD.JPY 161.7 → usd_per_unit[JPY] = 0.0061845. MSFT control in USD.
  const USD_PER_UNIT = { USD: 1, JPY: 0.0061845 };
  const MIXED: PortfolioData = {
    bankroll: 250_000,
    positions: [
      {
        id: 1,
        ticker: "5016",
        structure: "Stock (100 shares)",
        structure_type: "Stock",
        risk_profile: "equity",
        direction: "LONG",
        contracts: 100,
        expiry: "N/A",
        currency: "JPY",
        market_value: 526_700,
        legs: [
          {
            type: "Stock",
            direction: "LONG",
            strike: null,
            contracts: 100,
            avg_cost: 4_747,
            market_value: 526_700,
          },
        ],
      },
      {
        id: 2,
        ticker: "MSFT",
        structure: "Stock (100 shares)",
        structure_type: "Stock",
        risk_profile: "equity",
        direction: "LONG",
        contracts: 100,
        expiry: "N/A",
        market_value: 40_000,
        legs: [
          {
            type: "Stock",
            direction: "LONG",
            strike: null,
            contracts: 100,
            avg_cost: 380,
            market_value: 40_000,
          },
        ],
      },
    ],
    account_summary: {},
    exposure: {},
    violations: [],
  };

  it("converts native dollar-delta + market value to USD before summing", () => {
    const prices: Record<string, PriceData> = {
      "5016": makePriceData({ last: 5_267 }),
      MSFT: makePriceData({ last: 400 }),
    };
    const result = computeExposureDetailed(MIXED, prices, USD_PER_UNIT);

    // 5016: 100*5267*0.0061845 = 3257.43 ; MSFT: 100*400 = 40000
    const jp = result.rows.find((r) => r.ticker === "5016")!;
    expect(jp.dollarDelta).toBeCloseTo(3_257.43, 0);
    expect(jp.marketValue).toBeCloseTo(3_257.43, 0);

    expect(result.dollarDelta).toBeCloseTo(43_257.43, 0);
    expect(result.netLong).toBeCloseTo(43_257.43, 0);
    // (43257.43 / 250000) * 100 ≈ 17.30% — NOT 210%+ (the native-leak bug).
    expect(result.netExposurePct).toBeCloseTo(17.3, 0);
    expect(result.netExposurePct).toBeLessThan(50);
  });
});

describe("positionDeltaForHeader — known/unknown signal", () => {
  it("returns known=true when every leg has usable pricing data", () => {
    const prices: Record<string, PriceData> = {
      AAPL: makePriceData({ last: 260 }),
      AAPL_20260417_270_C: makePriceData({ last: 6.3, delta: 0.36 }),
      AAPL_20260417_290_C: makePriceData({ last: 2.13, delta: 0.08 }),
    };
    const pos = SPREAD_POSITION.positions[0];
    const out = positionDeltaForHeader(pos, prices);
    expect(out.known).toBe(true);
    expect(out.signed).toBeCloseTo(2800, 0);
  });

  it("returns known=false (with partial sum) when any leg lacks quote+spot", () => {
    // No AAPL spot, no per-leg delta → both legs fall back to the missing branch
    const out = positionDeltaForHeader(SPREAD_POSITION.positions[0], {});
    expect(out.known).toBe(false);
    expect(out.signed).toBe(0); // partial sum (both legs contributed 0)
  });

  it("preserves sign rules — SHORT Call contributes negative", () => {
    const prices: Record<string, PriceData> = {
      AAPL: makePriceData({ last: 260 }),
      AAPL_20260417_270_C: makePriceData({ delta: 0.36 }),
      AAPL_20260417_290_C: makePriceData({ delta: 0.08 }),
    };
    const out = positionDeltaForHeader(SPREAD_POSITION.positions[0], prices);
    // LONG 0.36*100*100 + SHORT -0.08*100*100 = 3600 - 800 = 2800
    expect(out.signed).toBeCloseTo(2800, 0);
    expect(out.known).toBe(true);
  });
});
