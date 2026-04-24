import { describe, expect, test } from "vitest";

import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { getAccountDayPnlFormula, resolveAccountDayPnlValue } from "@/components/MetricCards";

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
  timestamp: "2026-04-21T14:30:00.000Z",
  ...overrides,
});

const FUTU_PORTFOLIO: PortfolioData = {
  source: "futu",
  bankroll: 148_000,
  peak_value: 148_000,
  last_sync: "2026-04-21T14:30:00.000Z",
  positions: [
    {
      id: 1,
      ticker: "TSLA",
      structure: "Stock",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "",
      contracts: 300,
      direction: "LONG",
      entry_cost: 96_213,
      max_risk: null,
      market_value: 105_501,
      legs: [
        {
          conId: null,
          direction: "LONG",
          contracts: 300,
          type: "Stock",
          strike: null,
          entry_cost: 96_213,
          avg_cost: 320.71,
          market_price: 351.67,
          market_value: 105_501,
          market_price_is_calculated: false,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-04-01",
    },
  ],
  total_deployed_pct: 0,
  total_deployed_dollars: 105_501,
  remaining_capacity_pct: 100,
  position_count: 1,
  defined_risk_count: 0,
  undefined_risk_count: 1,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 148_000,
    daily_pnl: 9_288,
    unrealized_pnl: 9_288,
    realized_pnl: 0,
    settled_cash: -14_585,
    maintenance_margin: 114_285,
    excess_liquidity: 33_715,
    buying_power: 29_917,
    dividends: null,
  },
};

describe("resolveAccountDayPnlValue", () => {
  test("uses IB account_summary.daily_pnl for IB portfolios", () => {
    const ibPortfolio: PortfolioData = {
      ...FUTU_PORTFOLIO,
      source: "ib",
      account_summary: {
        ...FUTU_PORTFOLIO.account_summary!,
        daily_pnl: -1_234,
      },
    };

    expect(resolveAccountDayPnlValue(ibPortfolio, {})).toBe(-1_234);
  });

  test("uses computed intraday move for Futu instead of snapshot unrealized pnl", () => {
    const prices: Record<string, PriceData> = {
      TSLA: makePrice({
        symbol: "TSLA",
        last: 351.67,
        close: 350.67,
      }),
    };

    expect(resolveAccountDayPnlValue(FUTU_PORTFOLIO, prices)).toBe(300);
  });

  test("returns null for Futu when no intraday price data is available", () => {
    expect(resolveAccountDayPnlValue(FUTU_PORTFOLIO, {})).toBeNull();
  });
});

describe("getAccountDayPnlFormula", () => {
  test("describes live-price intraday math for Futu day pnl", () => {
    const formula = getAccountDayPnlFormula("futu");
    expect(formula).toContain("current_price");
    expect(formula).toContain("Futu positions + live realtime prices");
    expect(formula).not.toContain("Futu OpenD positions + account snapshot");
  });
});
