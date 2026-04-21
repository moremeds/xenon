import { describe, expect, test } from "vitest";
import { Value } from "@sinclair/typebox/value";
import { PortfolioDataSchema } from "@/lib/portfolioDataSchema";

describe("PortfolioDataSchema", () => {
  test("accepts the web portfolio shape used by /api/portfolio", () => {
    const sample = {
      source: "ib",
      bankroll: 100_000,
      peak_value: 102_500,
      last_sync: "2026-04-21T12:00:00Z",
      positions: [
        {
          id: 1,
          ticker: "SPY",
          structure: "Long Call",
          structure_type: "single",
          risk_profile: "defined",
          expiry: "2026-06-19",
          contracts: 1,
          direction: "LONG",
          entry_cost: 250,
          max_risk: 250,
          market_value: 300,
          legs: [
            {
              direction: "LONG",
              contracts: 1,
              type: "Call",
              strike: 600,
              entry_cost: 250,
              avg_cost: 250,
              market_price: 3,
              market_value: 300,
              market_price_is_calculated: false,
            },
          ],
          market_price_is_calculated: false,
          ib_daily_pnl: 15,
          kelly_optimal: 0.04,
          target: 500,
          stop: 150,
          entry_date: "2026-04-20",
        },
      ],
      total_deployed_pct: 2.5,
      total_deployed_dollars: 2_500,
      remaining_capacity_pct: 97.5,
      position_count: 1,
      defined_risk_count: 1,
      undefined_risk_count: 0,
      avg_kelly_optimal: 0.04,
      account_summary: {
        net_liquidation: 100_000,
        daily_pnl: null,
        unrealized_pnl: 500,
        realized_pnl: 0,
        settled_cash: 90_000,
        maintenance_margin: 5_000,
        excess_liquidity: 95_000,
        buying_power: 190_000,
        dividends: null,
      },
    };

    expect(Value.Check(PortfolioDataSchema, sample)).toBe(true);
  });
});
