import { describe, it, expect } from "vitest";
import {
  scoreVix,
  scoreVvix,
  scoreCorrelation,
  scoreMomentum,
  criLevel,
  computeCri,
} from "../lib/criCalc";

describe("scoreVix", () => {
  it("returns 0 for VIX at 15 (floor)", () => {
    expect(scoreVix(15, 0)).toBe(0);
  });

  it("returns 15 for VIX level at 40 (ceiling) with 0 ROC", () => {
    expect(scoreVix(40, 0)).toBe(15);
  });

  it("adds ROC component for positive ROC", () => {
    const score = scoreVix(15, 60); // level=0, roc=10
    expect(score).toBe(10);
  });

  it("clamps at 25", () => {
    expect(scoreVix(50, 100)).toBe(25);
  });

  it("returns 0 for NaN input", () => {
    expect(scoreVix(NaN, 10)).toBe(0);
    expect(scoreVix(20, NaN)).toBe(0);
  });
});

describe("scoreVvix", () => {
  it("returns 0 for VVIX at 90 (floor)", () => {
    expect(scoreVvix(90, 5)).toBe(0);
  });

  it("returns 17 for VVIX at 140 with ratio at floor", () => {
    expect(scoreVvix(140, 5)).toBe(17);
  });

  it("returns 0 for NaN", () => {
    expect(scoreVvix(NaN, 6)).toBe(0);
  });
});

describe("scoreCorrelation", () => {
  it("returns 0 for COR at 25 (floor)", () => {
    expect(scoreCorrelation(25, 0)).toBe(0);
  });

  it("scores spike component independently", () => {
    const score = scoreCorrelation(25, 20); // level=0, spike=8
    expect(score).toBe(8);
  });

  it("handles NaN correlation gracefully", () => {
    expect(scoreCorrelation(NaN, 10)).toBe(0);
  });

  it("handles NaN change by treating as 0", () => {
    const score = scoreCorrelation(50, NaN);
    // level = ((50-25)/(70-25)) * 17 ≈ 9.44, spike = 0
    expect(score).toBeCloseTo(9.4, 0);
  });
});

describe("scoreMomentum", () => {
  it("returns 0 for positive distance (above MA)", () => {
    expect(scoreMomentum(5)).toBe(0);
  });

  it("returns 0 for zero distance", () => {
    expect(scoreMomentum(0)).toBe(0);
  });

  it("scores linearly for negative distance", () => {
    // -5% → (5/10) * 25 = 12.5
    expect(scoreMomentum(-5)).toBe(12.5);
  });

  it("clamps at 25 for -10%+", () => {
    expect(scoreMomentum(-10)).toBe(25);
    expect(scoreMomentum(-15)).toBe(25);
  });

  it("returns 0 for NaN", () => {
    expect(scoreMomentum(NaN)).toBe(0);
  });
});

describe("criLevel", () => {
  it("classifies score ranges correctly", () => {
    expect(criLevel(0)).toBe("LOW");
    expect(criLevel(24.9)).toBe("LOW");
    expect(criLevel(25)).toBe("ELEVATED");
    expect(criLevel(49.9)).toBe("ELEVATED");
    expect(criLevel(50)).toBe("HIGH");
    expect(criLevel(74.9)).toBe("HIGH");
    expect(criLevel(75)).toBe("CRITICAL");
    expect(criLevel(100)).toBe("CRITICAL");
  });
});

describe("computeCri", () => {
  it("computes composite score from all components", () => {
    const result = computeCri({
      vix: 30,
      vix5dRoc: 30,
      vvix: 120,
      vvixVixRatio: 6.5,
      corr: 50,
      corr5dChange: 10,
      spxDistancePct: -3,
    });
    expect(result.score).toBeGreaterThan(0);
    expect(result.score).toBeLessThanOrEqual(100);
    expect(result.level).toBeDefined();
    expect(result.components.vix).toBeGreaterThan(0);
    expect(result.components.vvix).toBeGreaterThan(0);
    expect(result.components.correlation).toBeGreaterThan(0);
    expect(result.components.momentum).toBeGreaterThan(0);
  });

  it("returns LOW for calm market", () => {
    const result = computeCri({
      vix: 12,
      vix5dRoc: 0,
      vvix: 80,
      vvixVixRatio: 4,
      corr: 20,
      corr5dChange: 0,
      spxDistancePct: 3,
    });
    expect(result.score).toBe(0);
    expect(result.level).toBe("LOW");
  });

  it("returns CRITICAL for extreme stress", () => {
    const result = computeCri({
      vix: 50,
      vix5dRoc: 80,
      vvix: 150,
      vvixVixRatio: 10,
      corr: 80,
      corr5dChange: 25,
      spxDistancePct: -12,
    });
    expect(result.score).toBe(100);
    expect(result.level).toBe("CRITICAL");
  });
});
