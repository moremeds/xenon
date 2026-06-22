import { describe, expect, test } from "vitest";

import { computeDayMoveBreakdown } from "../lib/dayMoveBreakdown";
import type { PriceData } from "../lib/pricesProtocol";
import type { PortfolioData, PortfolioPosition } from "../lib/types";

/**
 * FX-awareness of the Day Move aggregator. Foreign positions report
 * `ib_daily_pnl` (and native-venue quotes) in JPY/KRW; the cross-position
 * `total` must convert each contribution to USD BEFORE summing, or a ¥/₩-sized
 * day P&L corrupts the USD headline (the observed −$552,137 DAY MOVE bug).
 *
 * Real 2026-06-22 IB snapshot (no synthetic data):
 *   5016  JX Advanced Metals (TSEJ/JPY): 100 sh, close ¥4,747, last ¥5,267
 *   000660 SK Hynix (KRX/KRW): 1 sh, close ₩2,764,000, last ₩2,885,000
 *   USD.JPY 161.7 → usd_per_unit[JPY] = 0.0061845
 *   USD.KRW 1537  → usd_per_unit[KRW] = 0.0006507
 */
const USD_PER_UNIT = { USD: 1, JPY: 0.0061845, KRW: 0.0006507 };

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
  timestamp: "2026-06-22T06:00:00.000Z",
  ...overrides,
});

const stock = (
  over: Partial<PortfolioPosition> &
    Pick<PortfolioPosition, "id" | "ticker" | "contracts">,
): PortfolioPosition => ({
  structure: "Stock",
  structure_type: "Stock",
  risk_profile: "equity",
  expiry: "N/A",
  direction: "LONG",
  entry_cost: 0,
  max_risk: null,
  market_value: null,
  ib_daily_pnl: null,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-05-01",
  legs: [],
  ...over,
});

const portfolio: PortfolioData = {
  bankroll: 250_000,
  peak_value: 250_000,
  last_sync: "2026-06-22T06:00:00.000Z",
  base_currency: "USD",
  fx_rates: USD_PER_UNIT,
  fx_unconverted_count: 0,
  total_deployed_pct: 0,
  total_deployed_dollars: 0,
  remaining_capacity_pct: 100,
  position_count: 3,
  defined_risk_count: 0,
  undefined_risk_count: 3,
  avg_kelly_optimal: null,
  positions: [
    // Japan: native day P&L (¥5,267−¥4,747)×100 = ¥52,000 reported by IB.
    stock({
      id: 1,
      ticker: "5016",
      contracts: 100,
      currency: "JPY",
      ib_daily_pnl: 52_000,
    }),
    // Korea: native day P&L (₩2,885,000−₩2,764,000)×1 = ₩121,000 reported by IB.
    stock({
      id: 2,
      ticker: "000660",
      contracts: 1,
      currency: "KRW",
      ib_daily_pnl: 121_000,
    }),
    // US control: no currency, WS close-based +$200.
    stock({ id: 3, ticker: "AAPL", contracts: 100 }),
  ],
  account_summary: {
    net_liquidation: 250_000,
    daily_pnl: 0,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 250_000,
    maintenance_margin: 0,
    excess_liquidity: 250_000,
    buying_power: 500_000,
    dividends: null,
  },
};

const prices: Record<string, PriceData> = {
  "5016": makePrice({ symbol: "5016", last: 5_267, close: 4_747 }),
  "000660": makePrice({ symbol: "000660", last: 2_885_000, close: 2_764_000 }),
  AAPL: makePrice({ symbol: "AAPL", last: 202, close: 200 }),
};

describe("computeDayMoveBreakdown — FX conversion of foreign day P&L", () => {
  test("converts each native day-P&L to USD before summing the total", () => {
    const { rows, total } = computeDayMoveBreakdown(
      portfolio,
      prices,
      USD_PER_UNIT,
    );

    // 52000*0.0061845 + 121000*0.0006507 + 200 = 321.594 + 78.7347 + 200
    expect(total).toBeCloseTo(600.33, 1);
    // Sanity: the BUG shape (native leaked into USD) would be ~173,200.
    expect(total).toBeLessThan(2_000);

    const jp = rows.find((r) => r.ticker === "5016")!;
    expect(jp.pnl).toBeCloseTo(321.59, 1); // USD, not ¥52,000
    const kr = rows.find((r) => r.ticker === "000660")!;
    expect(kr.pnl).toBeCloseTo(78.73, 1); // USD, not ₩121,000
    const us = rows.find((r) => r.ticker === "AAPL")!;
    expect(us.pnl).toBeCloseTo(200, 5);
  });

  test("labels foreign price columns with the native currency symbol, not $", () => {
    const { rows } = computeDayMoveBreakdown(portfolio, prices, USD_PER_UNIT);
    const jp = rows.find((r) => r.ticker === "5016")!;
    expect(jp.col1).toContain("¥4,747");
    expect(jp.col2).toContain("¥5,267");
    const kr = rows.find((r) => r.ticker === "000660")!;
    expect(kr.col2).toContain("₩2,885,000");
    const us = rows.find((r) => r.ticker === "AAPL")!;
    expect(us.col1).toContain("$200");
  });

  test("excludes a foreign position when no FX rate is available (no native leak)", () => {
    const { rows, total } = computeDayMoveBreakdown(portfolio, prices, {
      USD: 1,
    });
    // JP + KR drop out (no rate); only the USD control remains.
    expect(rows.map((r) => r.ticker)).toEqual(["AAPL"]);
    expect(total).toBeCloseTo(200, 5);
  });
});
