import { describe, it, expect } from "vitest";
import { fuseVirtualPair } from "@/lib/portfolioByStructure";
import type { PortfolioPosition, PortfolioLeg } from "@/lib/types";

function mkLeg(overrides: Partial<PortfolioLeg> = {}): PortfolioLeg {
  return {
    direction: "LONG",
    contracts: 10,
    type: "Put",
    strike: 400,
    entry_cost: 4000,
    avg_cost: 40,
    market_price: 38,
    market_value: 3800,
    market_price_is_calculated: false,
    ...overrides,
  };
}

function mkPos(
  id: number,
  leg: PortfolioLeg,
  overrides: Partial<PortfolioPosition> = {},
): PortfolioPosition {
  return {
    id,
    ticker: "TSLA",
    structure: `${leg.direction === "LONG" ? "Long" : "Short"} ${leg.type}`,
    structure_type: `${leg.direction === "LONG" ? "Long" : "Short"} ${leg.type}`,
    risk_profile: leg.direction === "LONG" ? "limited_risk" : "unlimited_risk",
    expiry: "2027-01-15",
    contracts: leg.contracts,
    direction: leg.direction,
    entry_cost: leg.entry_cost,
    max_risk: null,
    market_value: leg.market_value,
    legs: [leg],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "2026-01-01",
    ...overrides,
  };
}

describe("fuseVirtualPair", () => {
  const pair = {
    pairKey: "vp-1",
    label: "Bull Put Spread $390/$400 · 2027-01-15",
  };

  it("fuses a Bull Put Spread (Short $400 / Long $390) as CREDIT", () => {
    const shortLeg = mkPos(
      1,
      mkLeg({
        direction: "SHORT",
        type: "Put",
        strike: 400,
        contracts: 10,
        entry_cost: -7000,
        market_value: -6800,
      }),
    );
    const longLeg = mkPos(
      2,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 390,
        contracts: 10,
        entry_cost: 4000,
        market_value: 3800,
      }),
    );
    const fused = fuseVirtualPair(shortLeg, longLeg, pair, 0);
    expect(fused.id).toBeLessThan(0);
    expect(fused.ticker).toBe("TSLA");
    expect(fused.expiry).toBe("2027-01-15");
    expect(fused.contracts).toBe(10);
    expect(fused.structure_type).toBe("Bull Put Spread");
    expect(fused.direction).toBe("CREDIT");
    expect(fused.entry_cost).toBe(-3000);
    expect(fused.market_value).toBe(-3000);
    expect(fused.legs).toHaveLength(2);
    expect(fused.legs[0].direction).toBe("LONG");
    expect(fused.legs[1].direction).toBe("SHORT");
  });

  it("fuses a Bear Put Spread (Long $400 / Short $390) as DEBIT", () => {
    const longLeg = mkPos(
      3,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 400,
        contracts: 5,
        entry_cost: 2000,
        market_value: 1800,
      }),
    );
    const shortLeg = mkPos(
      4,
      mkLeg({
        direction: "SHORT",
        type: "Put",
        strike: 390,
        contracts: 5,
        entry_cost: -500,
        market_value: -400,
      }),
    );
    const fused = fuseVirtualPair(
      longLeg,
      shortLeg,
      { pairKey: "vp-2", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      1,
    );
    expect(fused.structure_type).toBe("Bear Put Spread");
    expect(fused.direction).toBe("DEBIT");
    expect(fused.entry_cost).toBe(1500);
    expect(fused.market_value).toBe(1400);
  });

  it("fuses a Long Straddle in strike-ascending leg order", () => {
    const callLeg = mkPos(
      5,
      mkLeg({
        direction: "LONG",
        type: "Call",
        strike: 400,
        contracts: 3,
        entry_cost: 900,
        market_value: 1200,
      }),
    );
    const putLeg = mkPos(
      6,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 400,
        contracts: 3,
        entry_cost: 600,
        market_value: 500,
      }),
    );
    const fused = fuseVirtualPair(
      callLeg,
      putLeg,
      { pairKey: "vp-3", label: "Long Straddle $400 · 2027-01-15" },
      2,
    );
    expect(fused.structure_type).toBe("Long Straddle");
    expect(fused.direction).toBe("DEBIT");
    expect(fused.legs.map((l) => l.strike)).toEqual([400, 400]);
  });

  it("propagates null market_value via sumOrNull", () => {
    const a = mkPos(
      7,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 400,
        entry_cost: 1000,
        market_value: null,
      }),
    );
    const b = mkPos(
      8,
      mkLeg({
        direction: "SHORT",
        type: "Put",
        strike: 390,
        entry_cost: -500,
        market_value: -200,
      }),
    );
    const fused = fuseVirtualPair(
      a,
      b,
      { pairKey: "vp-4", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      3,
    );
    expect(fused.market_value).toBe(-200);
  });

  it("returns null market_value when both legs are null", () => {
    const a = mkPos(
      9,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 400,
        market_value: null,
      }),
    );
    const b = mkPos(
      10,
      mkLeg({
        direction: "SHORT",
        type: "Put",
        strike: 390,
        market_value: null,
      }),
    );
    const fused = fuseVirtualPair(
      a,
      b,
      { pairKey: "vp-5", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      4,
    );
    expect(fused.market_value).toBeNull();
  });

  it("picks earliest non-empty entry_date; empty string when both empty", () => {
    const a = mkPos(
      11,
      mkLeg({ direction: "LONG", type: "Put", strike: 400 }),
      { entry_date: "" },
    );
    const b = mkPos(
      12,
      mkLeg({ direction: "SHORT", type: "Put", strike: 390 }),
      { entry_date: "2026-03-10" },
    );
    const fused = fuseVirtualPair(
      a,
      b,
      { pairKey: "vp-6", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      5,
    );
    expect(fused.entry_date).toBe("2026-03-10");
    const c = mkPos(
      13,
      mkLeg({ direction: "LONG", type: "Put", strike: 400 }),
      { entry_date: "" },
    );
    const d = mkPos(
      14,
      mkLeg({ direction: "SHORT", type: "Put", strike: 390 }),
      { entry_date: "" },
    );
    const fused2 = fuseVirtualPair(
      c,
      d,
      { pairKey: "vp-7", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      6,
    );
    expect(fused2.entry_date).toBe("");
  });

  it("emits FLAT direction when net entry_cost is exactly zero", () => {
    const longLeg = mkPos(
      20,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 400,
        contracts: 1,
        entry_cost: 500,
        market_value: 400,
      }),
    );
    const shortLeg = mkPos(
      21,
      mkLeg({
        direction: "SHORT",
        type: "Put",
        strike: 390,
        contracts: 1,
        entry_cost: -500,
        market_value: -300,
      }),
    );
    const fused = fuseVirtualPair(
      longLeg,
      shortLeg,
      { pairKey: "vp-flat", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      10,
    );
    expect(fused.entry_cost).toBe(0);
    expect(fused.direction).toBe("FLAT");
  });

  it("fuses a Synthetic (Long Call + Short Put, same strike) without $lo/$hi range in label", () => {
    const longCall = mkPos(
      22,
      mkLeg({
        direction: "LONG",
        type: "Call",
        strike: 400,
        contracts: 1,
        entry_cost: 300,
        market_value: 350,
      }),
    );
    const shortPut = mkPos(
      23,
      mkLeg({
        direction: "SHORT",
        type: "Put",
        strike: 400,
        contracts: 1,
        entry_cost: -200,
        market_value: -180,
      }),
    );
    const fused = fuseVirtualPair(
      longCall,
      shortPut,
      { pairKey: "vp-syn", label: "Synthetic $400 · 2027-01-15" },
      11,
    );
    expect(fused.structure_type).toBe("Synthetic");
    // structure should reuse pair.label — no $400/$400 range artifact
    expect(fused.structure).toBe("TSLA Synthetic $400 · 2027-01-15");
  });

  it("throws when given an invalid leg combination (same type + same direction)", () => {
    const a = mkPos(24, mkLeg({ direction: "LONG", type: "Put", strike: 400 }));
    const b = mkPos(25, mkLeg({ direction: "LONG", type: "Put", strike: 390 }));
    expect(() =>
      fuseVirtualPair(a, b, { pairKey: "vp-bad", label: "bogus" }, 12),
    ).toThrow(/unexpected leg combination/);
  });

  it("synthetic ids from different syntheticIdSeq values do not collide", () => {
    const a = mkPos(15, mkLeg({ direction: "LONG", type: "Put", strike: 400 }));
    const b = mkPos(
      16,
      mkLeg({ direction: "SHORT", type: "Put", strike: 390 }),
    );
    const f1 = fuseVirtualPair(a, b, { pairKey: "vp-8", label: "x" }, 0);
    const f2 = fuseVirtualPair(a, b, { pairKey: "vp-9", label: "y" }, 1);
    expect(f1.id).not.toBe(f2.id);
    expect(f1.id).toBeLessThan(0);
    expect(f2.id).toBeLessThan(0);
  });
});
