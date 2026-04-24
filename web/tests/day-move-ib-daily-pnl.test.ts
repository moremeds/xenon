import { describe, expect, test } from "vitest";

import { computeDayMoveBreakdown } from "../lib/dayMoveBreakdown";
import type { PriceData } from "../lib/pricesProtocol";
import type { PortfolioData } from "../lib/types";

const makePrice = (overrides: Partial<PriceData>): PriceData => ({
  symbol: "TEST",
  last: null,
  lastIsCalculated: false,
  bid: null,
  ask: null,
  bidSize: null,
  askSize: null,
  volume: null,
  high: null,
  low: null,
  open: null,
  close: null,
  week52High: null,
  week52Low: null,
  avgVolume: null,
  delta: null,
  gamma: null,
  theta: null,
  vega: null,
  impliedVol: null,
  undPrice: null,
  timestamp: "2026-03-19T18:30:00.000Z",
  ...overrides,
});

const portfolio: PortfolioData = {
  bankroll: 1_089_652.28,
  peak_value: 1_089_652.28,
  last_sync: "2026-03-19T18:30:00.000Z",
  total_deployed_pct: 0,
  total_deployed_dollars: 0,
  remaining_capacity_pct: 100,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  positions: [
    {
      id: 23,
      ticker: "WULF",
      structure: "Long Call",
      structure_type: "Long Call",
      risk_profile: "defined",
      expiry: "2027-01-15",
      contracts: 77,
      direction: "LONG",
      entry_cost: 40_076.51,
      max_risk: 40_076.51,
      market_value: 34_457.5,
      ib_daily_pnl: -3_405.31,
      legs: [
        {
          direction: "LONG",
          contracts: 77,
          type: "Call",
          strike: 17,
          entry_cost: 40_076.51,
          avg_cost: 520.4741844,
          market_price: 4.475,
          market_value: 34_457.5,
          market_price_is_calculated: false,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-03-19",
    },
  ],
  account_summary: {
    net_liquidation: 1_089_652.28,
    daily_pnl: -58_090.38,
    unrealized_pnl: -374_253.59,
    realized_pnl: 0,
    settled_cash: 206_956.63,
    maintenance_margin: 248_269.61,
    excess_liquidity: 474_890.55,
    buying_power: 1_899_562.19,
    dividends: 0,
  },
};

describe("computeDayMoveBreakdown — IB daily P&L for blended option positions", () => {
  test("prefers ib_daily_pnl over websocket close-based math when the signs disagree", () => {
    const prices: Record<string, PriceData> = {
      WULF_20270115_17_C: makePrice({
        symbol: "WULF_20270115_17_C",
        last: 4.475,
        bid: 4.45,
        ask: 4.5,
        close: 4.41,
      }),
    };

    const { rows, total } = computeDayMoveBreakdown(portfolio, prices);

    expect(rows).toHaveLength(1);
    expect(rows[0].ticker).toBe("WULF");
    expect(rows[0].pnl).toBe(-3_405.31);
    expect(total).toBe(-3_405.31);

    const wsOnlyPnl = (4.475 - 4.41) * 77 * 100;
    expect(wsOnlyPnl).toBeGreaterThan(0);
    expect(rows[0].pnl).not.toBeCloseTo(wsOnlyPnl, 2);
  });
});
