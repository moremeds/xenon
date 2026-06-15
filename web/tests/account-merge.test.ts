/**
 * @vitest-environment jsdom
 */

import { describe, it, expect } from "vitest";
import {
  accountMetrics,
  mergeAccountMetrics,
  type AccountMetrics,
} from "@/lib/accountMerge";
import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

function ibPortfolio(): PortfolioData {
  return {
    source: "ib",
    bankroll: 1_000_000,
    peak_value: 1_000_000,
    last_sync: "2026-06-15T14:00:00Z",
    positions: [],
    total_deployed_pct: 1.3,
    total_deployed_dollars: 13267,
    remaining_capacity_pct: 98.7,
    position_count: 2,
    defined_risk_count: 0,
    undefined_risk_count: 0,
    avg_kelly_optimal: null,
    account_summary: {
      net_liquidation: 1_003_726,
      daily_pnl: -461,
      unrealized_pnl: 0,
      realized_pnl: 0,
      settled_cash: 990_229,
      maintenance_margin: 0,
      excess_liquidity: 0,
      buying_power: 0,
      dividends: null,
      cash: 990_229,
    },
  };
}

function futuPortfolio(): PortfolioData {
  return {
    source: "futu",
    bankroll: 233_563,
    peak_value: 233_563,
    last_sync: "2026-06-02T00:01:17Z",
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
        entry_cost: 96213,
        max_risk: null,
        market_value: 105501,
        legs: [
          {
            direction: "LONG",
            contracts: 300,
            type: "Stock",
            strike: null,
            entry_cost: 96213,
            avg_cost: 320.71,
            market_price: 351.67,
            market_value: 105501,
            market_price_is_calculated: false,
          },
        ],
        kelly_optimal: null,
        target: null,
        stop: null,
        entry_date: "2026-04-01",
      },
    ],
    total_deployed_pct: 71.2,
    total_deployed_dollars: 550_326,
    remaining_capacity_pct: 28.8,
    position_count: 39,
    defined_risk_count: 0,
    undefined_risk_count: 1,
    avg_kelly_optimal: null,
    account_summary: {
      net_liquidation: 233_563,
      daily_pnl: 9288,
      unrealized_pnl: 9288,
      realized_pnl: 0,
      settled_cash: -24_797,
      maintenance_margin: 0,
      excess_liquidity: 0,
      buying_power: 0,
      dividends: null,
    },
  };
}

const PRICES: Record<string, PriceData> = {
  TSLA: {
    symbol: "TSLA",
    last: 351.67,
    lastIsCalculated: false,
    bid: 351.5,
    ask: 351.8,
    bidSize: 1,
    askSize: 1,
    volume: 100,
    high: null,
    low: null,
    open: null,
    close: 350.67,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: null,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: null,
    undPrice: null,
    timestamp: "2026-06-15T14:00:00Z",
  },
};

describe("accountMetrics", () => {
  it("extracts IB metrics (Today P&L = streamed daily_pnl)", () => {
    const m = accountMetrics(ibPortfolio());
    expect(m).toEqual<AccountMetrics>({
      netLiq: 1_003_726,
      todayPnl: -461,
      openRisk: 13267,
      cash: 990_229,
    });
  });

  it("extracts FUTU metrics (Today P&L = intraday from prices, not snapshot daily_pnl)", () => {
    const m = accountMetrics(futuPortfolio(), PRICES);
    expect(m.netLiq).toBe(233_563);
    expect(m.openRisk).toBe(550_326);
    expect(m.cash).toBe(-24_797); // falls back to settled_cash, sign preserved
    expect(m.todayPnl).toBe(300); // (351.67 - 350.67) * 300, NOT 9288
  });

  it("returns null metrics for a null portfolio", () => {
    expect(accountMetrics(null)).toEqual<AccountMetrics>({
      netLiq: null,
      todayPnl: null,
      openRisk: null,
      cash: null,
    });
  });

  it("returns null Today P&L for FUTU without prices", () => {
    expect(accountMetrics(futuPortfolio()).todayPnl).toBeNull();
  });
});

describe("mergeAccountMetrics", () => {
  it("sums each field across accounts, preserving sign", () => {
    const merged = mergeAccountMetrics([
      { netLiq: 1_003_726, todayPnl: -461, openRisk: 13267, cash: 990_229 },
      { netLiq: 233_563, todayPnl: 300, openRisk: 550_326, cash: -24_797 },
    ]);
    expect(merged).toEqual<AccountMetrics>({
      netLiq: 1_237_289,
      todayPnl: -161,
      openRisk: 563_593,
      cash: 965_432,
    });
  });

  it("treats null components as skipped, not zero", () => {
    const merged = mergeAccountMetrics([
      { netLiq: 100, todayPnl: 10, openRisk: 5, cash: 50 },
      { netLiq: 200, todayPnl: null, openRisk: 3, cash: null },
    ]);
    expect(merged.netLiq).toBe(300);
    expect(merged.todayPnl).toBe(10); // second null skipped
    expect(merged.openRisk).toBe(8);
    expect(merged.cash).toBe(50); // second null skipped
  });

  it("returns null for a field when every account is null", () => {
    const merged = mergeAccountMetrics([
      { netLiq: null, todayPnl: null, openRisk: null, cash: null },
      { netLiq: 200, todayPnl: null, openRisk: null, cash: null },
    ]);
    expect(merged.netLiq).toBe(200);
    expect(merged.todayPnl).toBeNull();
    expect(merged.openRisk).toBeNull();
    expect(merged.cash).toBeNull();
  });

  it("returns all-null when given no accounts", () => {
    expect(mergeAccountMetrics([])).toEqual<AccountMetrics>({
      netLiq: null,
      todayPnl: null,
      openRisk: null,
      cash: null,
    });
  });
});
