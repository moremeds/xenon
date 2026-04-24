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
  return {
    last: null,
    bid: null,
    ask: null,
    close: null,
    volume: null,
    ...overrides,
  };
}

let idCounter = 1000;
function nextId() {
  return ++idCounter;
}

function mkStock(
  ticker: string,
  contracts: number,
  marketValue: number | null,
  entryCost = 0,
): PortfolioPosition {
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

function mkVertical(
  ticker: string,
  opts: {
    mv: number;
    ec: number;
    structure?: string;
    structure_type?: string;
  } = { mv: 0, ec: 0 },
): PortfolioPosition {
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
      {
        type: "Call",
        direction: "LONG",
        strike: 100,
        contracts: 1,
        avg_cost: opts.ec,
        entry_cost: opts.ec,
        market_price: null,
        market_value: opts.mv,
      },
      {
        type: "Call",
        direction: "SHORT",
        strike: 110,
        contracts: 1,
        avg_cost: 0,
        entry_cost: 0,
        market_price: null,
        market_value: 0,
      },
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
      {
        type: "Call",
        direction: "SHORT",
        strike: 120,
        contracts: 1,
        avg_cost: -200,
        entry_cost: -200,
        market_price: null,
        market_value: -100,
      },
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
      {
        ...mkStock("Y", 10, null),
        legs: [
          {
            type: "Stock" as const,
            direction: "LONG" as const,
            strike: null,
            contracts: 10,
            avg_cost: 0,
            entry_cost: 0,
            market_price: null,
            market_value: null,
          },
        ],
      },
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
        {
          type: "Put",
          direction: "SHORT",
          strike: 440,
          contracts: 1,
          avg_cost: -500,
          entry_cost: -500,
          market_price: null,
          market_value: -200,
        },
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
        {
          type: "Put",
          direction: "SHORT",
          strike: 440,
          contracts: 1,
          avg_cost: -500,
          entry_cost: -500,
          market_price: null,
          market_value: -200,
        },
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
        {
          type: "Call",
          direction: "LONG",
          strike: 500,
          contracts: 1,
          avg_cost: 200,
          entry_cost: 200,
          market_price: null,
          market_value: 300,
        },
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
  function mkSingle(
    ticker: string,
    opts: {
      type: "Call" | "Put";
      dir: "LONG" | "SHORT";
      strike: number;
      expiry?: string;
      mv?: number;
      ec?: number;
    },
  ): PortfolioPosition {
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
      mkSingle("NVDA", {
        type: "Put",
        dir: "SHORT",
        strike: 175,
        mv: -7_538,
        ec: -9_285,
      }),
      mkSingle("NVDA", {
        type: "Put",
        dir: "LONG",
        strike: 170,
        mv: 3_983,
        ec: 5_340,
      }),
    ];
    const groups = buildTickerGroups(positions, {});
    const cats = groups[0].optionsByCategory;
    expect(cats.get("vertical")?.length).toBe(2);
    expect(cats.has("single")).toBe(false);
    // Both legs share a pair key + label
    const verticals = cats.get("vertical")!;
    const pair0 = groups[0].virtualPairs.get(verticals[0].id);
    const pair1 = groups[0].virtualPairs.get(verticals[1].id);
    expect(pair0?.pairKey).toBeTruthy();
    expect(pair0?.pairKey).toBe(pair1?.pairKey);
    // Long 170, Short 175 → Bull Put Spread
    expect(pair0?.label).toMatch(/Bull Put Spread/);
    expect(pair0?.label).toContain("$170");
    expect(pair0?.label).toContain("$175");
  });

  it("two TSLA Put spreads at same expiry → two distinct pair keys (tight-strike matching)", () => {
    // Simulates two Bull Put Spreads at the same expiry. Pair-by-strike
    // ensures we don't cross legs of different spreads.
    const positions = [
      mkSingle("TSLA", { type: "Put", dir: "LONG", strike: 340 }),
      mkSingle("TSLA", { type: "Put", dir: "LONG", strike: 360 }),
      mkSingle("TSLA", { type: "Put", dir: "SHORT", strike: 350 }),
      mkSingle("TSLA", { type: "Put", dir: "SHORT", strike: 370 }),
    ];
    const groups = buildTickerGroups(positions, {});
    const verticals = groups[0].optionsByCategory.get("vertical")!;
    expect(verticals.length).toBe(4);
    // Collect pair keys
    const pairs = new Set<string>();
    for (const v of verticals) {
      const pk = groups[0].virtualPairs.get(v.id)?.pairKey;
      if (pk) pairs.add(pk);
    }
    expect(pairs.size).toBe(2);
    // Tight matching: L340+S350 and L360+S370 (sorted by strike)
    const pairLabels = new Set<string>();
    for (const v of verticals) {
      const label = groups[0].virtualPairs.get(v.id)?.label;
      if (label) pairLabels.add(label);
    }
    expect(
      Array.from(pairLabels).some(
        (l) => l.includes("$340") && l.includes("$350"),
      ),
    ).toBe(true);
    expect(
      Array.from(pairLabels).some(
        (l) => l.includes("$360") && l.includes("$370"),
      ),
    ).toBe(true);
  });

  it("Bull Call Spread via separate legs: Long Call + Short Call same expiry → vertical", () => {
    const positions = [
      mkSingle("SPY", { type: "Call", dir: "LONG", strike: 500 }),
      mkSingle("SPY", { type: "Call", dir: "SHORT", strike: 520 }),
    ];
    expect(
      buildTickerGroups(positions, {})[0].optionsByCategory.get("vertical")
        ?.length,
    ).toBe(2);
  });

  it("Long Call + Long Put same strike → straddle; different strikes → strangle", () => {
    const same = [
      mkSingle("X", { type: "Call", dir: "LONG", strike: 100 }),
      mkSingle("X", { type: "Put", dir: "LONG", strike: 100 }),
    ];
    expect(
      buildTickerGroups(same, {})[0].optionsByCategory.get("straddle")?.length,
    ).toBe(2);

    const diff = [
      mkSingle("Y", { type: "Call", dir: "LONG", strike: 110 }),
      mkSingle("Y", { type: "Put", dir: "LONG", strike: 90 }),
    ];
    expect(
      buildTickerGroups(diff, {})[0].optionsByCategory.get("strangle")?.length,
    ).toBe(2);
  });

  it("Long Call + Short Put (risk reversal / synthetic long) → synthetic", () => {
    const positions = [
      mkSingle("Z", { type: "Call", dir: "LONG", strike: 100 }),
      mkSingle("Z", { type: "Put", dir: "SHORT", strike: 100 }),
    ];
    expect(
      buildTickerGroups(positions, {})[0].optionsByCategory.get("synthetic")
        ?.length,
    ).toBe(2);
  });

  it("different expiries do NOT pair (one stays single)", () => {
    const positions = [
      mkSingle("A", {
        type: "Put",
        dir: "LONG",
        strike: 100,
        expiry: "2026-05-15",
      }),
      mkSingle("A", {
        type: "Put",
        dir: "SHORT",
        strike: 95,
        expiry: "2026-06-19",
      }),
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

  it("contract count mismatch: Long 10 + Short 5 → NOT paired (both stay single)", () => {
    const long10: PortfolioPosition = {
      ...mkSingle("X", { type: "Put", dir: "LONG", strike: 100 }),
      contracts: 10,
      legs: [
        {
          type: "Put",
          direction: "LONG",
          strike: 100,
          contracts: 10,
          avg_cost: 100,
          entry_cost: 1000,
          market_price: null,
          market_value: 1200,
        },
      ],
    };
    const short5: PortfolioPosition = {
      ...mkSingle("X", { type: "Put", dir: "SHORT", strike: 110 }),
      contracts: 5,
      legs: [
        {
          type: "Put",
          direction: "SHORT",
          strike: 110,
          contracts: 5,
          avg_cost: -200,
          entry_cost: -1000,
          market_price: null,
          market_value: -600,
        },
      ],
    };
    const cats = buildTickerGroups([long10, short5], {})[0].optionsByCategory;
    expect(cats.has("vertical")).toBe(false);
    expect(cats.get("single")?.length).toBe(2);
  });

  it("null-strike candidates are rejected from pairing (never $0 labels)", () => {
    const nullStrike: PortfolioPosition = {
      ...mkSingle("X", { type: "Put", dir: "LONG", strike: 100 }),
      legs: [
        {
          type: "Put",
          direction: "LONG",
          strike: null,
          contracts: 1,
          avg_cost: 100,
          entry_cost: 100,
          market_price: null,
          market_value: 120,
        },
      ],
    };
    const real = mkSingle("X", { type: "Put", dir: "SHORT", strike: 110 });
    const cats = buildTickerGroups([nullStrike, real], {})[0].optionsByCategory;
    // No vertical: strike-less leg cannot pair
    expect(cats.has("vertical")).toBe(false);
    expect(cats.get("single")?.length).toBe(2);
  });

  it("straddle pass is now strike-sorted: two LONG straddles at 100 and 120 pair correctly", () => {
    // Inputs deliberately out of strike order to catch the index-based pairing bug.
    const positions = [
      mkSingle("Y", { type: "Call", dir: "LONG", strike: 120 }),
      mkSingle("Y", { type: "Put", dir: "LONG", strike: 100 }),
      mkSingle("Y", { type: "Put", dir: "LONG", strike: 120 }),
      mkSingle("Y", { type: "Call", dir: "LONG", strike: 100 }),
    ];
    const cats = buildTickerGroups(positions, {})[0].optionsByCategory;
    // After strike-sort, same-strike pairs → two straddles
    expect(cats.get("straddle")?.length).toBe(4);
    expect(cats.get("strangle")).toBeUndefined();
    // Both distinct pair keys (not one merged pair)
    const pairs = new Set<string>();
    for (const pos of cats.get("straddle")!) {
      const pk = (
        buildTickerGroups(positions, {})[0].virtualPairs.get(pos.id) as {
          pairKey: string;
        }
      )?.pairKey;
      if (pk) pairs.add(pk);
    }
    expect(pairs.size).toBe(2);
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
        {
          type: "Call",
          direction: "LONG",
          strike: 100,
          contracts: 1,
          avg_cost: 800,
          entry_cost: 800,
          market_price: null,
          market_value: 1000,
        },
        {
          type: "Call",
          direction: "SHORT",
          strike: 110,
          contracts: 1,
          avg_cost: -300,
          entry_cost: -300,
          market_price: null,
          market_value: -300,
        },
      ],
    };
    const groups = buildTickerGroups([combo], {});
    expect(groups[0].optionsByCategory.get("vertical")?.length).toBe(1);
  });
});

describe("buildTickerGroups — partial aggregation guards (tribunal fixes)", () => {
  it("totalPnlPct is null when any P&L contributor was null-skipped (denominator/numerator cohort match)", () => {
    // One resolved position (legs MV present) + one unresolved (all MVs null).
    const ok = mkStock("Q", 10, 1_000, 900);
    const broken: PortfolioPosition = {
      ...mkStock("Q", 10, null, 500),
      legs: [
        {
          type: "Stock",
          direction: "LONG",
          strike: null,
          contracts: 10,
          avg_cost: 50,
          entry_cost: 500,
          market_price: null,
          market_value: null,
        },
      ],
    };
    const groups = buildTickerGroups([ok, broken], {});
    const agg = groups[0].agg;
    expect(agg.totalPnl).toBe(100); // only the resolved one contributes: 1000 - 900
    // Denominator dilution would have given ≠ null; strict policy → null.
    expect(agg.totalPnlPct).toBe(null);
  });

  it("netDelta is null when ANY contributor had known=false (strict policy, not just all-unknown)", () => {
    // Priced leg for one vertical + unpriced leg for another
    const knownPos = mkVertical("K", { mv: 100, ec: 90 });
    const unknownPos = mkVertical("K", { mv: 50, ec: 40 });
    const prices: Record<string, PriceData> = {
      K: mkPrice({ last: 105 }),
      [`K_20260515_100_C`]: mkPrice({ delta: 0.6 }),
      [`K_20260515_110_C`]: mkPrice({ delta: 0.3 }),
      // No price entries for the second vertical's strikes → its legs fall back to 0 with known=false
    };
    // But wait, both verticals share the same strike keys in mkVertical. So both get deltas.
    // Instead: use a naked single with no delta alongside a known vertical.
    const groups = buildTickerGroups([knownPos, unknownPos], prices);
    // Both verticals resolve via the same keys → both known. Expect non-null.
    expect(groups[0].agg.netDelta).not.toBe(null);
  });

  it("netDelta strict: a known position on ticker M + an unknown position on same ticker → null", () => {
    // Build known: ticker M vertical with both per-leg deltas supplied
    const knownPos = mkVertical("M", { mv: 100, ec: 90 });
    // Build unknown: a single on ticker M2 (different ticker → own bucket) won't help;
    // instead craft an unknown leg on M with an empty-string expiry so the strike
    // check path isn't available and no per-leg delta is present.
    const unknownSingle: PortfolioPosition = {
      id: nextId(),
      ticker: "M",
      structure: "Long Call $200.0",
      structure_type: "Long Call",
      risk_profile: "defined",
      direction: "DEBIT",
      contracts: 1,
      expiry: "2026-05-15",
      entry_cost: 100,
      market_value: 120,
      legs: [
        {
          type: "Call",
          direction: "LONG",
          strike: 200,
          contracts: 1,
          avg_cost: 100,
          entry_cost: 100,
          market_price: null,
          market_value: 120,
        },
      ],
    };
    // No M spot at all → spot resolution fails for the single → missingLegs > 0
    // Per-leg deltas only for the vertical's two strikes.
    const prices: Record<string, PriceData> = {
      [`M_20260515_100_C`]: mkPrice({ delta: 0.6 }),
      [`M_20260515_110_C`]: mkPrice({ delta: 0.3 }),
      // M (spot) missing, M_20260515_200_C missing → approx delta can't compute
    };
    const groups = buildTickerGroups([knownPos, unknownSingle], prices);
    expect(groups[0].agg.netDelta).toBe(null);
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
    const groups = buildTickerGroups(
      [mkVertical("KKK", { mv: 100, ec: 90 })],
      prices,
    );
    expect(groups[0].agg.netDelta).not.toBe(null);
    // LONG 0.6*100 + SHORT -0.3*100 = 60 - 30 = 30
    expect(groups[0].agg.netDelta).toBeCloseTo(30, 5);
  });
});

describe("buildTickerGroups with fuseVirtualPairs: true", () => {
  beforeEach(() => __resetMissWarningsForTests());

  function mkSingle(
    ticker: string,
    opts: {
      type: "Call" | "Put";
      dir: "LONG" | "SHORT";
      strike: number;
      expiry?: string;
      mv?: number;
      ec?: number;
    },
  ): PortfolioPosition {
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

  it("replaces two paired single-leg positions with one fused multi-leg position", () => {
    const longPut = mkSingle("TSLA", {
      type: "Put",
      dir: "LONG",
      strike: 400,
      expiry: "2027-01-15",
      mv: 3800,
      ec: 4000,
    });
    const shortPut = mkSingle("TSLA", {
      type: "Put",
      dir: "SHORT",
      strike: 390,
      expiry: "2027-01-15",
      mv: -200,
      ec: -500,
    });
    // Ensure contract counts match so the pair detector will pair them.
    shortPut.legs[0].contracts = longPut.legs[0].contracts;
    shortPut.contracts = longPut.contracts;

    const [group] = buildTickerGroups([longPut, shortPut], undefined, {
      fuseVirtualPairs: true,
    });
    const verticals = group.optionsByCategory.get("vertical") ?? [];

    expect(verticals).toHaveLength(1);
    expect(verticals[0].legs).toHaveLength(2);
    expect(verticals[0].id).toBeLessThan(0);
    expect(verticals[0].structure_type).toBe("Bear Put Spread");
    expect(verticals[0].direction).toBe("DEBIT");
    expect(group.virtualPairs.has(verticals[0].id)).toBe(true);
    expect(group.virtualPairs.has(longPut.id)).toBe(false);
  });

  it("preserves ticker header aggregates (sum-invariant)", () => {
    const longPut = mkSingle("TSLA", {
      type: "Put",
      dir: "LONG",
      strike: 400,
      expiry: "2027-01-15",
      mv: 3800,
      ec: 4000,
    });
    const shortPut = mkSingle("TSLA", {
      type: "Put",
      dir: "SHORT",
      strike: 390,
      expiry: "2027-01-15",
      mv: -200,
      ec: -500,
    });
    shortPut.legs[0].contracts = longPut.legs[0].contracts;
    shortPut.contracts = longPut.contracts;

    const baseline = buildTickerGroups([longPut, shortPut]);
    const fused = buildTickerGroups([longPut, shortPut], undefined, {
      fuseVirtualPairs: true,
    });

    expect(fused[0].agg.mv).toBe(baseline[0].agg.mv);
    expect(fused[0].agg.totalPnl).toBe(baseline[0].agg.totalPnl);
    expect(fused[0].agg.entryCost).toBe(baseline[0].agg.entryCost);
  });

  it("leaves unpaired single legs as single-leg positions", () => {
    const loneShort = mkSingle("TSLA", {
      type: "Put",
      dir: "SHORT",
      strike: 400,
      expiry: "2027-01-15",
      mv: -100,
      ec: -200,
    });

    const [group] = buildTickerGroups([loneShort], undefined, {
      fuseVirtualPairs: true,
    });
    const singles = group.optionsByCategory.get("single") ?? [];
    expect(singles).toHaveLength(1);
    expect(singles[0].id).toBe(loneShort.id);
    expect(singles[0].legs).toHaveLength(1);
  });

  it("default (fuseVirtualPairs omitted) produces unchanged output shape", () => {
    const longPut = mkSingle("TSLA", {
      type: "Put",
      dir: "LONG",
      strike: 400,
      expiry: "2027-01-15",
      mv: 3800,
      ec: 4000,
    });
    const shortPut = mkSingle("TSLA", {
      type: "Put",
      dir: "SHORT",
      strike: 390,
      expiry: "2027-01-15",
      mv: -200,
      ec: -500,
    });
    shortPut.legs[0].contracts = longPut.legs[0].contracts;
    shortPut.contracts = longPut.contracts;

    const [group] = buildTickerGroups([longPut, shortPut]);
    const verticals = group.optionsByCategory.get("vertical") ?? [];
    expect(verticals.map((p) => p.id).sort()).toEqual(
      [longPut.id, shortPut.id].sort(),
    );
  });

  it("lands a fused Synthetic under the 'synthetic' category (not 'other')", () => {
    // Long Call + Short Put at the same strike & expiry → detector
    // classifies this as category "synthetic", but the fused structure_type
    // "Synthetic" has no catalog entry. The fusion path must preserve the
    // detector's category so the row doesn't fall through to "other".
    const longCall = mkSingle("TSLA", {
      type: "Call",
      dir: "LONG",
      strike: 400,
      expiry: "2027-01-15",
      mv: 350,
      ec: 300,
    });
    const shortPut = mkSingle("TSLA", {
      type: "Put",
      dir: "SHORT",
      strike: 400,
      expiry: "2027-01-15",
      mv: -180,
      ec: -200,
    });
    shortPut.legs[0].contracts = longCall.legs[0].contracts;
    shortPut.contracts = longCall.contracts;

    const [group] = buildTickerGroups([longCall, shortPut], undefined, {
      fuseVirtualPairs: true,
    });
    const synthetic = group.optionsByCategory.get("synthetic") ?? [];
    const other = group.optionsByCategory.get("other") ?? [];
    expect(synthetic).toHaveLength(1);
    expect(synthetic[0].structure_type).toBe("Synthetic");
    expect(other).toHaveLength(0);
  });
});
