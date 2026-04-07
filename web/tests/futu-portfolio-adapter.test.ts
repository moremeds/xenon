import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import { join } from "path";
import {
  futuToPortfolioData,
  futuToAccountSummary,
  type FutuPortfolioEnvelope,
  type FutuRawPosition,
} from "@/lib/futuPortfolioAdapter";

const REAL_JSON_PATH = join(__dirname, "..", "..", "data", "futu_portfolio.json");

function loadReal(): FutuPortfolioEnvelope {
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
    const stock = data.positions.find((p) => p.ticker === "TSLA" && p.structure === "Stock");
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

  it("formats option expiry as YYYY-MM-DD", () => {
    const opt = data.positions.find((p) => p.expiry.length > 0);
    if (opt) {
      expect(opt.expiry).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    }
  });
});

describe("futuToPortfolioData — aggregates", () => {
  it("sets bankroll to the envelope net_liquidation", () => {
    const env = loadReal();
    const data = futuToPortfolioData(env);
    expect(data.bankroll).toBe(env.account_summary.net_liquidation);
  });

  it("exposes total_deployed_dollars from sum of |market_value|", () => {
    const env = loadReal();
    const data = futuToPortfolioData(env);
    const expected = env.positions.reduce(
      (sum: number, p: FutuRawPosition) => sum + Math.abs(p.market_value ?? 0),
      0,
    );
    expect(data.total_deployed_dollars).toBeCloseTo(expected, 2);
  });

  it("last_sync is the envelope fetched_at (UTC ISO Z)", () => {
    const env = loadReal();
    const data = futuToPortfolioData(env);
    expect(data.last_sync).toBe(env.fetched_at);
    expect(data.last_sync).toMatch(/Z$/);
  });
});
