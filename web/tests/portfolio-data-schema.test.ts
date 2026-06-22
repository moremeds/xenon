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

  test("accepts a JPY position with USD fields + fx_rates", () => {
    // Real IB snapshot 2026-06-22: 5016 JX Advanced Metals, 100 sh.
    //   entry 100 @ prior-close ¥4,747 = ¥474,700 → $2,936.49
    //   mkt   100 @ last ¥5,267       = ¥526,700 → $3,258.17
    //   USD.JPY 161.6575 → usd_per_unit[JPY] = 0.006186
    const payload = {
      bankroll: 100_000,
      peak_value: 100_000,
      last_sync: "2026-06-22T00:00:00Z",
      base_currency: "USD",
      fx_rates: { USD: 1, JPY: 0.006186 },
      fx_unconverted_count: 0,
      positions: [
        {
          id: 1,
          ticker: "5016",
          currency: "JPY",
          exchange: "TSEJ",
          structure: "Stock (100 shares)",
          structure_type: "Stock",
          risk_profile: "equity",
          expiry: "N/A",
          contracts: 100,
          direction: "LONG",
          entry_cost: 474_700,
          entry_cost_usd: 2_936.49,
          max_risk: null,
          market_value: 526_700,
          market_value_usd: 3_258.17,
          legs: [
            {
              direction: "LONG",
              contracts: 100,
              type: "Stock",
              currency: "JPY",
              strike: null,
              entry_cost: 474_700,
              avg_cost: 4_747,
              market_price: 5_267,
              market_value: 526_700,
              market_value_usd: 3_258.17,
            },
          ],
          kelly_optimal: null,
          target: null,
          stop: null,
          entry_date: "unknown",
        },
      ],
      total_deployed_pct: 2.94,
      total_deployed_dollars: 2_936.49,
      remaining_capacity_pct: 97.06,
      position_count: 1,
      defined_risk_count: 0,
      undefined_risk_count: 1,
      avg_kelly_optimal: null,
    };
    expect(Value.Check(PortfolioDataSchema, payload)).toBe(true);
  });
});
