/**
 * Unit tests for `buildTickerGroups` (Portfolio By-Structure pure grouping).
 *
 * Scope — pure function only. The React component that consumes the output
 * is tested separately in `portfolio-by-structure-component.test.tsx`.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { buildTickerGroups } from "@/lib/portfolioByStructure";
import { __resetMissWarningsForTests } from "@/lib/structureCatalog";
import type { PortfolioPosition, PortfolioLeg } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

function mkPrice(overrides: Partial<PriceData> = {}): PriceData {
  return { last: null, bid: null, ask: null, close: null, volume: null, ...overrides };
}

let idCounter = 1000;
function nextId() {
  return ++idCounter;
}

function mkStock(ticker: string, contracts: number, marketValue: number | null, entryCost = 0): PortfolioPosition {
  const leg: PortfolioLeg = {
    type: "Stock",
    direction: "LONG",
    strike: null,
    contracts,
    avg_cost: 0,
    entry_cost: entryCost,
    market_price: null,
    market_value: marketValue,
  };
  return {
    id: nextId(),
    ticker,
    structure: `Stock (${contracts} shares)`,
    structure_type: "Stock",
    risk_profile: "equity",
    direction: "LONG",
    contracts,
    expiry: "N/A",
    entry_cost: entryCost,
    market_value: marketValue,
    legs: [leg],
  };
}

function mkVertical(ticker: string, opts: { mv: number; ec: number; structure?: string; structure_type?: string } = { mv: 0, ec: 0 }): PortfolioPosition {
  return {
    id: nextId(),
    ticker,
    structure: opts.structure ?? "Bull Call Spread $100.0/$110.0",
    structure_type: opts.structure_type ?? "Bull Call Spread",
    risk_profile: "defined",
    direction: "DEBIT",
    contracts: 1,
    expiry: "2026-05-15",
    entry_cost: opts.ec,
    market_value: opts.mv,
    legs: [
      { type: "Call", direction: "LONG", strike: 100, contracts: 1, avg_cost: opts.ec, entry_cost: opts.ec, market_price: null, market_value: opts.mv },
      { type: "Call", direction: "SHORT", strike: 110, contracts: 1, avg_cost: 0, entry_cost: 0, market_price: null, market_value: 0 },
    ],
  };
}

function mkCovered(ticker: string): PortfolioPosition {
  return {
    id: nextId(),
    ticker,
    structure: "Covered Call",
    structure_type: "Covered Call",
    risk_profile: "defined",
    direction: "CREDIT",
    contracts: 1,
    expiry: "2026-05-15",
    entry_cost: -200,
    market_value: -100,
    legs: [
      { type: "Call", direction: "SHORT", strike: 120, contracts: 1, avg_cost: -200, entry_cost: -200, market_price: null, market_value: -100 },
    ],
  };
}

beforeEach(() => {
  __resetMissWarningsForTests();
});

describe("buildTickerGroups — grouping", () => {
  it("buckets 1 stock + 2 verticals + 1 covered into one TickerGroup with per-category counts", () => {
    const positions = [
      mkStock("TSLA", 100, 30_000),
      mkVertical("TSLA", { mv: 500, ec: 400 }),
      mkVertical("TSLA", { mv: 800, ec: 600 }),
      mkCovered("TSLA"),
    ];
    const groups = buildTickerGroups(positions, {});
    expect(groups).toHaveLength(1);
    const tsla = groups[0];
    expect(tsla.ticker).toBe("TSLA");
    expect(tsla.stock).not.toBeNull();
    expect(tsla.optionsByCategory.get("vertical")?.length).toBe(2);
    expect(tsla.optionsByCategory.get("covered")?.length).toBe(1);
    expect(tsla.optionsByCategory.has("single")).toBe(false);
  });

  it("sorts tickers by |MV| desc (50k / 200k / 10k → 200k, 50k, 10k)", () => {
    const positions = [
      mkStock("AAA", 1, 50_000),
      mkStock("BBB", 1, 200_000),
      mkStock("CCC", 1, 10_000),
    ];
    const groups = buildTickerGroups(positions, {});
    expect(groups.map((g) => g.ticker)).toEqual(["BBB", "AAA", "CCC"]);
  });

  it("stock-only ticker has empty optionsByCategory", () => {
    const groups = buildTickerGroups([mkStock("NVDA", 10, 5_000)], {});
    expect(groups).toHaveLength(1);
    expect(groups[0].stock).not.toBeNull();
    expect(groups[0].optionsByCategory.size).toBe(0);
  });

  it("iteration order of optionsByCategory respects CATEGORY_ORDER", () => {
    // covered before vertical in input; output should be vertical before covered
    const groups = buildTickerGroups(
      [mkCovered("MSFT"), mkVertical("MSFT", { mv: 100, ec: 90 })],
      {},
    );
    const keys = Array.from(groups[0].optionsByCategory.keys());
    expect(keys).toEqual(["vertical", "covered"]);
  });
});

describe("buildTickerGroups — aggregate null propagation", () => {
  it("excludes null-MV contributors from sum", () => {
    const positions = [
      mkStock("X", 10, 1_000),
      mkStock("X", 10, null), // null MV leg
    ];
    // Second stock ends up in options bucket (already has stock); both contribute
    // null for second → sum should be 1_000
    const groups = buildTickerGroups(positions, {});
    expect(groups[0].agg.mv).toBe(1_000);
  });

  it("all-null MV → agg.mv is null (NOT zero)", () => {
    const positions = [
      { ...mkStock("Y", 10, null), legs: [{ type: "Stock" as const, direction: "LONG" as const, strike: null, contracts: 10, avg_cost: 0, entry_cost: 0, market_price: null, market_value: null }] },
    ];
    const groups = buildTickerGroups(positions, {});
    expect(groups[0].agg.mv).toBe(null);
  });

  it("zero entry cost → totalPnlPct is null", () => {
    const positions = [mkStock("Z", 10, 1_000, 0)];
    const groups = buildTickerGroups(positions, {});
    expect(groups[0].agg.entryCost).toBe(0);
    expect(groups[0].agg.totalPnlPct).toBe(null);
  });
});

describe("buildTickerGroups — lookup + classifier regressions", () => {
  it("ISSUE-1 guard: raw IB shape (structure='Short Put $440.0', structure_type='Short Put') buckets to 'single' not 'other'", () => {
    const pos: PortfolioPosition = {
      id: nextId(),
      ticker: "TSLA",
      structure: "Short Put $440.0",
      structure_type: "Short Put",
      risk_profile: "undefined",
      direction: "CREDIT",
      contracts: 1,
      expiry: "2026-05-15",
      entry_cost: -500,
      market_value: -200,
      legs: [
        { type: "Put", direction: "SHORT", strike: 440, contracts: 1, avg_cost: -500, entry_cost: -500, market_price: null, market_value: -200 },
      ],
    };
    const groups = buildTickerGroups([pos], {});
    expect(groups[0].optionsByCategory.has("single")).toBe(true);
    expect(groups[0].optionsByCategory.has("other")).toBe(false);
  });

  it("stock discriminator is structure_type === 'Stock' — not risk_profile", () => {
    // A position tagged risk_profile=equity but structure_type=Vertical should
    // go into options, not stock.
    const weird: PortfolioPosition = {
      ...mkVertical("QQQ", { mv: 100, ec: 90 }),
      risk_profile: "equity",
    };
    const groups = buildTickerGroups([weird], {});
    expect(groups[0].stock).toBeNull();
    expect(groups[0].optionsByCategory.get("vertical")?.length).toBe(1);
  });

  it("sign-math regression: credit + debit on same ticker sums with signs preserved (no Math.abs)", () => {
    // Short Put: collected $500 premium, MV now $200 (less negative)
    const shortPut: PortfolioPosition = {
      id: nextId(),
      ticker: "TSLA",
      structure: "Short Put $440.0",
      structure_type: "Short Put",
      risk_profile: "undefined",
      direction: "CREDIT",
      contracts: 1,
      expiry: "2026-05-15",
      entry_cost: -500,
      market_value: -200,
      legs: [
        { type: "Put", direction: "SHORT", strike: 440, contracts: 1, avg_cost: -500, entry_cost: -500, market_price: null, market_value: -200 },
      ],
    };
    // Long Call: paid $200, MV now $300
    const longCall: PortfolioPosition = {
      id: nextId(),
      ticker: "TSLA",
      structure: "Long Call $500.0",
      structure_type: "Long Call",
      risk_profile: "defined",
      direction: "DEBIT",
      contracts: 1,
      expiry: "2026-05-15",
      entry_cost: 200,
      market_value: 300,
      legs: [
        { type: "Call", direction: "LONG", strike: 500, contracts: 1, avg_cost: 200, entry_cost: 200, market_price: null, market_value: 300 },
      ],
    };
    const groups = buildTickerGroups([shortPut, longCall], {});
    const agg = groups[0].agg;
    // Σ MV = -200 + 300 = 100
    expect(agg.mv).toBe(100);
    // Σ EC = -500 + 200 = -300
    expect(agg.entryCost).toBe(-300);
    // Σ totalPnl = (-200 - (-500)) + (300 - 200) = 300 + 100 = 400
    expect(agg.totalPnl).toBe(400);
    // % = 400 / |−300| × 100 = 133.33…
    expect(agg.totalPnlPct).toBeCloseTo(133.333, 2);
  });
});

describe("buildTickerGroups — virtual combo detection (orphan single legs)", () => {
  function mkSingle(ticker: string, opts: { type: "Call" | "Put"; dir: "LONG" | "SHORT"; strike: number; expiry?: string; mv?: number; ec?: number }): PortfolioPosition {
    const structureName = `${opts.dir === "LONG" ? "Long" : "Short"} ${opts.type}`;
    return {
      id: nextId(),
      ticker,
      structure: `${structureName} $${opts.strike}.0`,
      structure_type: structureName,
      risk_profile: opts.dir === "LONG" ? "defined" : "undefined",
      direction: opts.dir === "LONG" ? "DEBIT" : "CREDIT",
      contracts: 1,
      expiry: opts.expiry ?? "2026-05-15",
      entry_cost: opts.ec ?? (opts.dir === "LONG" ? 100 : -100),
      market_value: opts.mv ?? (opts.dir === "LONG" ? 120 : -80),
      legs: [
        {
          type: opts.type,
          direction: opts.dir,
          strike: opts.strike,
          contracts: 1,
          avg_cost: opts.ec ?? (opts.dir === "LONG" ? 100 : -100),
          entry_cost: opts.ec ?? (opts.dir === "LONG" ? 100 : -100),
          market_price: null,
          market_value: opts.mv ?? (opts.dir === "LONG" ? 120 : -80),
        },
      ],
    };
  }

  it("NVDA Long Put + Short Put same expiry → both reassigned to vertical (user-reported case)", () => {
    const positions = [
      mkSingle("NVDA", { type: "Put", dir: "SHORT", strike: 175, mv: -7_538, ec: -9_285 }),
      mkSingle("NVDA", { type: "Put", dir: "LONG", strike: 170, mv: 3_983, ec: 5_340 }),
    ];
    const groups = buildTickerGroups(positions, {});
    const cats = groups[0].optionsByCategory;
    expect(cats.get("vertical")?.length).toBe(2);
    expect(cats.has("single")).toBe(false);
  });

  it("Bull Call Spread via separate legs: Long Call + Short Call same expiry → vertical", () => {
    const positions = [
      mkSingle("SPY", { type: "Call", dir: "LONG", strike: 500 }),
      mkSingle("SPY", { type: "Call", dir: "SHORT", strike: 520 }),
    ];
    expect(buildTickerGroups(positions, {})[0].optionsByCategory.get("vertical")?.length).toBe(2);
  });

  it("Long Call + Long Put same strike → straddle; different strikes → strangle", () => {
    const same = [
      mkSingle("X", { type: "Call", dir: "LONG", strike: 100 }),
      mkSingle("X", { type: "Put", dir: "LONG", strike: 100 }),
    ];
    expect(buildTickerGroups(same, {})[0].optionsByCategory.get("straddle")?.length).toBe(2);

    const diff = [
      mkSingle("Y", { type: "Call", dir: "LONG", strike: 110 }),
      mkSingle("Y", { type: "Put", dir: "LONG", strike: 90 }),
    ];
    expect(buildTickerGroups(diff, {})[0].optionsByCategory.get("strangle")?.length).toBe(2);
  });

  it("Long Call + Short Put (risk reversal / synthetic long) → synthetic", () => {
    const positions = [
      mkSingle("Z", { type: "Call", dir: "LONG", strike: 100 }),
      mkSingle("Z", { type: "Put", dir: "SHORT", strike: 100 }),
    ];
    expect(buildTickerGroups(positions, {})[0].optionsByCategory.get("synthetic")?.length).toBe(2);
  });

  it("different expiries do NOT pair (one stays single)", () => {
    const positions = [
      mkSingle("A", { type: "Put", dir: "LONG", strike: 100, expiry: "2026-05-15" }),
      mkSingle("A", { type: "Put", dir: "SHORT", strike: 95, expiry: "2026-06-19" }),
    ];
    const cats = buildTickerGroups(positions, {})[0].optionsByCategory;
    expect(cats.has("vertical")).toBe(false);
    expect(cats.get("single")?.length).toBe(2);
  });

  it("odd unpaired leg stays in single (e.g. 3 longs + 1 short → 1 vertical pair + 2 singles)", () => {
    const positions = [
      mkSingle("B", { type: "Call", dir: "LONG", strike: 100 }),
      mkSingle("B", { type: "Call", dir: "LONG", strike: 110 }),
      mkSingle("B", { type: "Call", dir: "LONG", strike: 120 }),
      mkSingle("B", { type: "Call", dir: "SHORT", strike: 130 }),
    ];
    const cats = buildTickerGroups(positions, {})[0].optionsByCategory;
    expect(cats.get("vertical")?.length).toBe(2);
    expect(cats.get("single")?.length).toBe(2);
  });

  it("pre-classified multi-leg structures (real IB combos) are untouched by virtual-combo detection", () => {
    // A real IB Bull Call Spread with both legs inside a single position must
    // NOT trip the orphan detector — it already has structure_type='Bull Call Spread'.
    const combo: PortfolioPosition = {
      id: nextId(),
      ticker: "C",
      structure: "Bull Call Spread $100/$110",
      structure_type: "Bull Call Spread",
      risk_profile: "defined",
      direction: "DEBIT",
      contracts: 1,
      expiry: "2026-05-15",
      entry_cost: 500,
      market_value: 700,
      legs: [
        { type: "Call", direction: "LONG", strike: 100, contracts: 1, avg_cost: 800, entry_cost: 800, market_price: null, market_value: 1000 },
        { type: "Call", direction: "SHORT", strike: 110, contracts: 1, avg_cost: -300, entry_cost: -300, market_price: null, market_value: -300 },
      ],
    };
    const groups = buildTickerGroups([combo], {});
    expect(groups[0].optionsByCategory.get("vertical")?.length).toBe(1);
  });
});

describe("buildTickerGroups — netDelta known/unknown", () => {
  it("netDelta is null iff every contributor is unknown", () => {
    // No prices → both legs of the vertical fall back to 0 with known=false
    const groups = buildTickerGroups(
      [mkVertical("KKK", { mv: 100, ec: 90 })],
      {},
    );
    expect(groups[0].agg.netDelta).toBe(null);
  });

  it("netDelta is non-null (partial sum) when at least one contributor is known", () => {
    const prices: Record<string, PriceData> = {
      KKK: mkPrice({ last: 105 }),
      // provide per-leg delta for both legs so known=true
      KKK_20260515_100_C: mkPrice({ delta: 0.6 }),
      KKK_20260515_110_C: mkPrice({ delta: 0.3 }),
    };
    const groups = buildTickerGroups([mkVertical("KKK", { mv: 100, ec: 90 })], prices);
    expect(groups[0].agg.netDelta).not.toBe(null);
    // LONG 0.6*100 + SHORT -0.3*100 = 60 - 30 = 30
    expect(groups[0].agg.netDelta).toBeCloseTo(30, 5);
  });
});
