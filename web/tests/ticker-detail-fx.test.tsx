/**
 * @vitest-environment jsdom
 *
 * Ticker-detail panels (PositionTab + ActHeldSummary) must render foreign
 * positions in USD (native sub-line), not treat ¥/₩ magnitudes as USD.
 *
 * Real 2026-06-22 IB snapshot:
 *   5016  JX Advanced Metals (TSEJ/JPY): 100 sh, avg ¥4,747, last ¥5,267
 *         entry ¥474,700 ($2,936), MV ¥526,700 ($3,257); USD.JPY 161.7
 *   000660 SK Hynix (KRX/KRW): 1 sh, entry_cost_usd $1,798.5, market_value_usd $1,877.3
 */
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import PositionTab from "@/components/ticker-detail/PositionTab";
import ActHeldSummary from "@/components/ticker-detail/ActHeldSummary";
import type { PortfolioPosition } from "@/lib/types";
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

const jp5016: PortfolioPosition = {
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
  entry_cost_usd: 2_935.8,
  max_risk: null,
  market_value: 526_700,
  market_value_usd: 3_257.4,
  ib_daily_pnl: null,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-05-01",
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
      market_value_usd: 3_257.4,
    },
  ],
};

describe("PositionTab — foreign position renders USD with native sub-line", () => {
  it("converts entry cost / MV / P&L to USD via the live USD.JPY tick", () => {
    const prices: Record<string, PriceData> = {
      "USD.JPY": makePrice({ symbol: "USD.JPY", last: 161.7 }),
      "5016": makePrice({ symbol: "5016", last: 5_267, close: 4_747 }),
    };
    render(<PositionTab position={jp5016} prices={prices} />);

    // USD headlines (¥474,700 × 1/161.7 ≈ $2,936 ; ¥526,700 ≈ $3,257).
    expect(screen.getByText("$2,936")).toBeTruthy();
    expect(screen.getByText("$3,257")).toBeTruthy();
    expect(screen.getByText(/\+\$322/)).toBeTruthy();

    // Native sub-lines + native prices (¥), never the native magnitude as $.
    expect(screen.getByText(/¥474,700/)).toBeTruthy();
    expect(screen.getByText(/¥526,700/)).toBeTruthy();
    expect(screen.getByText(/¥4,747/)).toBeTruthy();
    expect(screen.getByText(/¥5,267/)).toBeTruthy();
    expect(screen.queryByText("$474,700")).toBeNull();
    expect(screen.queryByText("$526,700")).toBeNull();
  });
});

describe("ActHeldSummary — foreign P&L via *_usd siblings", () => {
  it("shows USD P&L from market_value_usd − entry_cost_usd, not native", () => {
    const krPos: PortfolioPosition = {
      id: 2,
      ticker: "000660",
      currency: "KRW",
      exchange: "KRX",
      structure: "Stock (1 share)",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "N/A",
      contracts: 1,
      direction: "LONG",
      entry_cost: 2_764_000,
      entry_cost_usd: 1_798.5,
      max_risk: null,
      market_value: 2_885_000,
      market_value_usd: 1_877.3,
      ib_daily_pnl: null,
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-05-01",
      legs: [
        {
          direction: "LONG",
          contracts: 1,
          type: "Stock",
          currency: "KRW",
          strike: null,
          entry_cost: 2_764_000,
          avg_cost: 2_764_000,
          market_price: 2_885_000,
          market_value: 2_885_000,
          market_value_usd: 1_877.3,
        },
      ],
    };
    render(<ActHeldSummary position={krPos} onOpenDeck={() => {}} />);

    // 1877.3 − 1798.5 = $78.8 → "$79"; never the native ₩121,000.
    expect(screen.getByText("$79")).toBeTruthy();
    expect(screen.queryByText(/121,000/)).toBeNull();
  });
});
