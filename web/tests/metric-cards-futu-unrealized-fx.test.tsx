/**
 * @vitest-environment jsdom
 *
 * The Futu ACCOUNT card's UNREALIZED P&L must be USD. account_summary.unrealized_pnl
 * is a NATIVE-currency sum for Futu (Futu's accinfo returns "N/A" for the USD
 * figure, so the backend falls back to summing per-position native unrealized) —
 * a ¥/₩ row leaks its native magnitude into the headline. MetricCards must
 * instead derive the Futu unrealized from the FX-converted per-position sum.
 *
 * Real-derived: TSLA stock unrealized +$9,288 (USD) + a JPY stock with ¥78,000
 * unrealized. USD.JPY 161.67 → ¥78,000 × (1/161.67) = $482.47.
 * Correct USD total = $9,770.47 — NOT the native-leaked $87,288.
 */
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render } from "@testing-library/react";
import MetricCards from "@/components/MetricCards";
import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

afterEach(() => cleanup());

const makePrice = (overrides: Partial<PriceData>): PriceData =>
  ({
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
  }) as PriceData;

const NATIVE_LEAK = 9288 + 78000; // 87,288 — the bug: ¥78,000 summed as $

const portfolio: PortfolioData = {
  source: "futu",
  bankroll: 200_000,
  peak_value: 200_000,
  last_sync: "2026-06-22T06:00:00.000Z",
  positions: [
    {
      id: 1,
      ticker: "TSLA",
      currency: "USD",
      structure: "Stock",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "",
      contracts: 300,
      direction: "LONG",
      entry_cost: 96_213,
      max_risk: null,
      market_value: 105_501, // +$9,288 unrealized
      legs: [
        {
          direction: "LONG",
          contracts: 300,
          type: "Stock",
          strike: null,
          currency: "USD",
          entry_cost: 96_213,
          avg_cost: 320.71,
          market_price: 351.67,
          market_value: 105_501,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-04-01",
    },
    {
      id: 2,
      ticker: "5016",
      currency: "JPY",
      structure: "Stock",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "",
      contracts: 100,
      direction: "LONG",
      entry_cost: 2_372_000,
      max_risk: null,
      market_value: 2_450_000, // ¥ — +¥78,000 unrealized
      legs: [
        {
          direction: "LONG",
          contracts: 100,
          type: "Stock",
          strike: null,
          currency: "JPY",
          entry_cost: 2_372_000,
          avg_cost: 23_720,
          market_price: 24_500,
          market_value: 2_450_000,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-04-01",
    },
  ],
  total_deployed_pct: 0,
  total_deployed_dollars: 120_653,
  remaining_capacity_pct: 100,
  position_count: 2,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 200_000,
    daily_pnl: 0,
    unrealized_pnl: NATIVE_LEAK, // native-leaked figure the card must NOT show
    realized_pnl: 0,
    settled_cash: 0,
    maintenance_margin: 0,
    excess_liquidity: 0,
    buying_power: 0,
    dividends: null,
  },
};

const prices: Record<string, PriceData> = {
  "USD.JPY": makePrice({ symbol: "USD.JPY", last: 161.67 }),
};

describe("MetricCards — Futu UNREALIZED P&L is USD, not a native sum", () => {
  it("shows the FX-converted per-position unrealized (~$9,770), never the native $87,288", () => {
    const { container } = render(
      <MetricCards
        portfolio={portfolio}
        prices={prices}
        realizedPnl={0}
        section="portfolio"
      />,
    );
    const unrealizedCard = Array.from(
      container.querySelectorAll(".metric-card"),
    ).find((el) => el.textContent?.includes("Unrealized P&L"));
    expect(unrealizedCard).toBeTruthy();
    // 9,288 (USD) + 78,000¥×(1/161.67)=482.47 → +$9,770.47
    expect(unrealizedCard?.textContent).toContain("9,770");
    // The native-leaked magnitude must not appear.
    expect(unrealizedCard?.textContent).not.toContain("87,288");
  });
});
