import { describe, it, expect } from "vitest";
import { existsSync, readFileSync } from "fs";
import { join } from "path";
import {
  futuToPortfolioData,
  futuToAccountSummary,
  type FutuPortfolioEnvelope,
  type FutuRawPosition,
} from "@/lib/futuPortfolioAdapter";

const REAL_JSON_PATH = join(
  __dirname,
  "..",
  "..",
  "data",
  "futu_portfolio.json",
);

function loadReal(): FutuPortfolioEnvelope {
  if (!existsSync(REAL_JSON_PATH)) {
    return {
      ok: true,
      fetched_at: "2026-04-20T14:54:40.996Z",
      data_as_of: "2026-04-20T14:54:40.996Z",
      account_id: "acct",
      source: "futu",
      is_stale: false,
      warnings: [],
      count: 3,
      positions: [
        {
          futu_code: "US.TSLA",
          normalized: {
            kind: "STK",
            symbol: "TSLA",
            exchange: "SMART",
            currency: "USD",
            live_data: true,
          },
          quantity: 100,
          avg_cost: 300,
          market_price: 320,
          market_value: 32000,
          unrealized_pnl: 2000,
          unrealized_pnl_pct: 6.67,
          currency: "USD",
          position_side: "LONG",
        },
        {
          futu_code: "US.TSLA270117C400000",
          normalized: {
            kind: "OPT",
            symbol: "TSLA",
            expiry: "20270117",
            strike: 400,
            right: "C",
            exchange: "SMART",
            currency: "USD",
            trading_class: null,
            live_data: false,
          },
          quantity: 1,
          avg_cost: 12.5,
          market_price: 13.1,
          market_value: 1310,
          unrealized_pnl: 60,
          unrealized_pnl_pct: 4.8,
          currency: "USD",
          position_side: "LONG",
        },
        {
          futu_code: "US.TSLA270117P300000",
          normalized: {
            kind: "OPT",
            symbol: "TSLA",
            expiry: "20270117",
            strike: 300,
            right: "P",
            exchange: "SMART",
            currency: "USD",
            trading_class: null,
            live_data: false,
          },
          quantity: -1,
          avg_cost: 8.25,
          market_price: 6.9,
          market_value: -690,
          unrealized_pnl: 135,
          unrealized_pnl_pct: 16.36,
          currency: "USD",
          position_side: "SHORT",
        },
      ],
      account_summary: {
        net_liquidation: 100_000,
        equity_with_loan: 100_000,
        cash: 10_000,
        settled_cash: 10_000,
        buying_power: 20_000,
        available_funds: 20_000,
        initial_margin: 5_000,
        maintenance_margin: 4_000,
        excess_liquidity: 16_000,
        gross_position_value: 34_000,
        unrealized_pnl: 2_195,
        daily_pnl: 100,
        realized_pnl: 0,
        dividends: null,
        previous_day_ewl: null,
        reg_t_equity: null,
        sma: null,
      },
    };
  }
  return JSON.parse(readFileSync(REAL_JSON_PATH, "utf-8"));
}

describe("futuToAccountSummary", () => {
  it("populates mappable fields from the envelope", () => {
    const env = loadReal();
    const s = futuToAccountSummary(env.account_summary);
    expect(s.net_liquidation).toBeGreaterThan(0);
    expect(s.buying_power).toBeGreaterThan(0);
    expect(s.maintenance_margin).toBeGreaterThan(0);
    expect(s.excess_liquidity).toBeGreaterThan(0);
  });

  it("leaves unmappable fields as null/undefined so MetricCards renders ---", () => {
    const env = loadReal();
    const s = futuToAccountSummary(env.account_summary);
    expect(s.dividends).toBeNull();
    expect(s.previous_day_ewl).toBeUndefined();
    expect(s.reg_t_equity).toBeUndefined();
    expect(s.sma).toBeUndefined();
  });

  it("aliases equity_with_loan to net_liquidation", () => {
    const env = loadReal();
    const s = futuToAccountSummary(env.account_summary);
    expect(s.equity_with_loan).toBe(s.net_liquidation);
  });

  it("aliases settled_cash to cash", () => {
    const env = loadReal();
    const s = futuToAccountSummary(env.account_summary);
    expect(s.settled_cash).toBe(s.cash);
  });
});

describe("futuToPortfolioData — position classification", () => {
  const env = loadReal();
  const data = futuToPortfolioData(env);

  it("emits one PortfolioPosition per futu row", () => {
    expect(data.position_count).toBe(env.count);
    expect(data.positions.length).toBe(env.count);
  });

  it("classifies long stock as equity with structure_type=Stock", () => {
    const stock = data.positions.find(
      (p) => p.ticker === "TSLA" && p.structure === "Stock",
    );
    expect(stock).toBeTruthy();
    expect(stock!.risk_profile).toBe("equity");
    expect(stock!.direction).toBe("LONG");
    // Load-bearing: WorkspaceShell.tsx:58 filters rows with
    // `structure_type === "Stock"` to route them to the stock WS bucket.
    // Lowercase "stock" silently broke this.
    expect(stock!.structure_type).toBe("Stock");
  });

  it("classifies long option as defined risk with structure_type='Long Call'", () => {
    const longCall = data.positions.find(
      (p) => p.ticker === "TSLA" && p.structure === "Long Call",
    );
    expect(longCall).toBeTruthy();
    expect(longCall!.risk_profile).toBe("defined");
    expect(longCall!.structure_type).toBe("Long Call");
  });

  it("classifies short option as undefined risk with structure_type='Short Put'", () => {
    // Verticals are not collapsed for Futu in v1 — each leg is classified
    // individually. That's intentional: portfolio-level risk aggregation is
    // an IB-sync-specific feature we're not reimplementing for read-only
    // Futu data.
    const shortPut = data.positions.find(
      (p) => p.ticker === "TSLA" && p.structure === "Short Put",
    );
    expect(shortPut).toBeTruthy();
    expect(shortPut!.risk_profile).toBe("undefined");
    expect(shortPut!.structure_type).toBe("Short Put");
  });

  it("renders UNKNOWN rows with raw code as ticker and structure_type='Unknown'", () => {
    const unknown = data.positions.find((p) => p.ticker.startsWith("HK."));
    if (unknown) {
      expect(unknown.structure).toBe("Unknown");
      expect(unknown.structure_type).toBe("Unknown");
      expect(unknown.risk_profile).toBe("complex");
    }
  });

  it("propagates native currency onto the position AND leg (drives FX display + subscription)", () => {
    const env: FutuPortfolioEnvelope = {
      ok: true,
      fetched_at: "2026-06-22T14:00:00.000Z",
      data_as_of: "2026-06-22T14:00:00.000Z",
      account_id: "acct",
      source: "futu",
      is_stale: false,
      warnings: [],
      count: 1,
      positions: [
        {
          futu_code: "JP.5016",
          normalized: {
            kind: "STK",
            symbol: "5016",
            exchange: "TSEJ",
            currency: "JPY",
            live_data: true,
          },
          quantity: 100,
          avg_cost: 24_500,
          market_price: 24_500,
          market_value: 2_450_000,
          unrealized_pnl: 0,
          unrealized_pnl_pct: 0,
          currency: "JPY",
          position_side: "LONG",
        },
      ],
      account_summary: {
        net_liquidation: 100_000,
        equity_with_loan: 100_000,
        cash: 0,
        settled_cash: 0,
        buying_power: 0,
        available_funds: 0,
        initial_margin: 0,
        maintenance_margin: 0,
        excess_liquidity: 0,
        gross_position_value: 15_152,
        unrealized_pnl: 0,
        daily_pnl: 0,
        realized_pnl: 0,
        dividends: null,
        previous_day_ewl: null,
        reg_t_equity: null,
        sma: null,
      },
    };
    const data = futuToPortfolioData(env);
    expect(data.positions[0].currency).toBe("JPY");
    expect(data.positions[0].legs[0].currency).toBe("JPY");
  });

  it("formats option expiry as YYYY-MM-DD", () => {
    const opt = data.positions.find((p) => p.expiry.length > 0);
    if (opt) {
      expect(opt.expiry).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    }
  });

  it("preserves short option credit sign in entry_cost so headline P&L stays correct", () => {
    const env: FutuPortfolioEnvelope = {
      ok: true,
      fetched_at: "2026-04-20T14:54:40.996Z",
      data_as_of: "2026-04-20T14:54:40.996Z",
      account_id: "acct",
      source: "futu",
      is_stale: false,
      warnings: [],
      count: 1,
      positions: [
        {
          futu_code: "US.TSLA270115P400000",
          normalized: {
            kind: "OPT",
            symbol: "TSLA",
            expiry: "20270115",
            strike: 400,
            right: "P",
            exchange: "SMART",
            currency: "USD",
            trading_class: null,
            live_data: false,
          },
          quantity: -1,
          avg_cost: 2.0,
          market_price: 1.7,
          market_value: -170,
          unrealized_pnl: 30,
          unrealized_pnl_pct: 15,
          currency: "USD",
          position_side: "SHORT",
        },
      ],
      account_summary: {
        net_liquidation: 100_000,
        equity_with_loan: 100_000,
        cash: 10_000,
        settled_cash: 10_000,
        buying_power: 20_000,
        available_funds: 20_000,
        initial_margin: 5_000,
        maintenance_margin: 4_000,
        excess_liquidity: 16_000,
        gross_position_value: 170,
        unrealized_pnl: 30,
        daily_pnl: 10,
        realized_pnl: 0,
        dividends: null,
        previous_day_ewl: null,
        reg_t_equity: null,
        sma: null,
      },
    };

    const data = futuToPortfolioData(env);
    const pos = data.positions[0];
    expect(pos.entry_cost).toBe(-200);
    expect(pos.legs[0].entry_cost).toBe(-200);
    expect(pos.market_value).toBe(-170);
    expect(pos.market_value! - pos.entry_cost).toBe(30);
  });
});

describe("futuToPortfolioData — aggregates", () => {
  it("sets bankroll to the envelope net_liquidation", () => {
    const env = loadReal();
    const data = futuToPortfolioData(env);
    expect(data.bankroll).toBe(env.account_summary.net_liquidation);
  });

  it("exposes total_deployed_dollars (OPEN RISK) from the USD gross_position_value", () => {
    const env = loadReal();
    const data = futuToPortfolioData(env);
    // gross_position_value comes from Futu's accinfo_query(currency=USD) — it is
    // already USD-denominated, unlike per-position market_value which is native.
    expect(data.total_deployed_dollars).toBeCloseTo(
      Math.abs(env.account_summary.gross_position_value ?? 0),
      2,
    );
  });

  it("does NOT inflate OPEN RISK by summing native ¥/₩ market_value (regression)", () => {
    // Real-derived: one USD short option (-$28,250 MV) + one JPY stock whose MV
    // is ¥2,450,000. The account aggregates (USD) give a $469,004 gross. The
    // bug summed |market_value| → $2,478,250, leaking the ¥ magnitude.
    const env: FutuPortfolioEnvelope = {
      ok: true,
      fetched_at: "2026-06-22T14:00:00.000Z",
      data_as_of: "2026-06-22T14:00:00.000Z",
      account_id: "acct",
      source: "futu",
      is_stale: false,
      warnings: [],
      count: 2,
      positions: [
        {
          futu_code: "US.TSLA260821P400000",
          normalized: {
            kind: "OPT",
            symbol: "TSLA",
            expiry: "20260821",
            strike: 400,
            right: "P",
            exchange: "SMART",
            currency: "USD",
            trading_class: null,
            live_data: false,
          },
          quantity: -1,
          avg_cost: 250,
          market_price: 282.5,
          market_value: -28_250,
          unrealized_pnl: 0,
          unrealized_pnl_pct: 0,
          currency: "USD",
          position_side: "SHORT",
        },
        {
          futu_code: "JP.5016",
          normalized: {
            kind: "STK",
            symbol: "5016",
            exchange: "TSEJ",
            currency: "JPY",
            live_data: true,
          },
          quantity: 100,
          avg_cost: 24_500,
          market_price: 24_500,
          market_value: 2_450_000, // ¥ — must not leak into the USD total
          unrealized_pnl: 0,
          unrealized_pnl_pct: 0,
          currency: "JPY",
          position_side: "LONG",
        },
      ],
      account_summary: {
        net_liquidation: 207_348.11,
        equity_with_loan: 207_348.11,
        cash: -27_859.82,
        settled_cash: -27_859.82,
        buying_power: 50_000,
        available_funds: 50_000,
        initial_margin: 100_000,
        maintenance_margin: 139_881.42,
        excess_liquidity: 67_466.69,
        gross_position_value: 469_004.47, // long_mv 352,106.20 + |short_mv| 116,898.27 (USD)
        unrealized_pnl: 0,
        daily_pnl: 0,
        realized_pnl: 0,
        dividends: null,
        previous_day_ewl: null,
        reg_t_equity: null,
        sma: null,
      },
    };

    const data = futuToPortfolioData(env);
    expect(data.total_deployed_dollars).toBeCloseTo(469_004.47, 2);
    expect(data.total_deployed_dollars).toBeLessThan(1_000_000);
    // Explicitly assert the old buggy native sum is gone.
    expect(data.total_deployed_dollars).not.toBeCloseTo(2_478_250, 2);
  });

  it("falls back to native |market_value| sum when gross_position_value is absent", () => {
    const env = loadReal();
    const envNoGross: FutuPortfolioEnvelope = {
      ...env,
      account_summary: { ...env.account_summary, gross_position_value: null },
    };
    const data = futuToPortfolioData(envNoGross);
    const nativeSum = env.positions.reduce(
      (sum: number, p: FutuRawPosition) => sum + Math.abs(p.market_value ?? 0),
      0,
    );
    expect(data.total_deployed_dollars).toBeCloseTo(nativeSum, 2);
  });

  it("last_sync is the envelope fetched_at (UTC ISO Z)", () => {
    const env = loadReal();
    const data = futuToPortfolioData(env);
    expect(data.last_sync).toBe(env.fetched_at);
    expect(data.last_sync).toMatch(/Z$/);
  });
});
