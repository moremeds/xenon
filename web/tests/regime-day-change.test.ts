/**
 * Unit tests: Regime strip day change display.
 *
 * Verifies that the day change indicator computes correctly
 * from WS last and close prices during market hours.
 */

import { describe, it, expect } from "vitest";

/** Replica of DayChange logic from RegimePanel.tsx */
function computeDayChange(last: number | null, close: number | null) {
  if (last == null || close == null || close <= 0) return null;
  const change = last - close;
  const pct = (change / close) * 100;
  return { change, pct, isUp: change >= 0 };
}

describe("Regime strip day change", () => {
  it("computes positive day change", () => {
    const result = computeDayChange(25.5, 24.0);
    expect(result).not.toBeNull();
    expect(result!.change).toBeCloseTo(1.5, 2);
    expect(result!.pct).toBeCloseTo(6.25, 2);
    expect(result!.isUp).toBe(true);
  });

  it("computes negative day change", () => {
    const result = computeDayChange(22.0, 24.0);
    expect(result).not.toBeNull();
    expect(result!.change).toBeCloseTo(-2.0, 2);
    expect(result!.pct).toBeCloseTo(-8.33, 2);
    expect(result!.isUp).toBe(false);
  });

  it("returns null when last is null", () => {
    expect(computeDayChange(null, 24.0)).toBeNull();
  });

  it("returns null when close is null", () => {
    expect(computeDayChange(25.0, null)).toBeNull();
  });

  it("returns null when close is zero", () => {
    expect(computeDayChange(25.0, 0)).toBeNull();
  });

  it("handles zero change (flat day)", () => {
    const result = computeDayChange(24.0, 24.0);
    expect(result).not.toBeNull();
    expect(result!.change).toBe(0);
    expect(result!.pct).toBe(0);
    expect(result!.isUp).toBe(true);
  });

  it("SPY with dollar prefix scenario", () => {
    // SPY: last=560.25, close=555.10
    const result = computeDayChange(560.25, 555.10);
    expect(result).not.toBeNull();
    expect(result!.change).toBeCloseTo(5.15, 2);
    expect(result!.pct).toBeCloseTo(0.928, 1);
    expect(result!.isUp).toBe(true);
  });
});

/** Replica of PointChange logic */
function computePointChange(change: number | null) {
  if (change == null || Math.abs(change) < 0.005) return null;
  return { change, isUp: change >= 0 };
}

describe("Regime strip point change (RVOL, COR1M)", () => {
  it("positive point change", () => {
    const result = computePointChange(6.88);
    expect(result).not.toBeNull();
    expect(result!.isUp).toBe(true);
    expect(result!.change).toBe(6.88);
  });

  it("negative point change", () => {
    const result = computePointChange(-2.5);
    expect(result).not.toBeNull();
    expect(result!.isUp).toBe(false);
  });

  it("returns null for negligible change", () => {
    expect(computePointChange(0.001)).toBeNull();
  });

  it("returns null for null input", () => {
    expect(computePointChange(null)).toBeNull();
  });

  it("RVOL intraday delta", () => {
    // intradayRvol=11.52, staticRvol=11.53 → change=-0.01
    const result = computePointChange(11.52 - 11.53);
    expect(result).not.toBeNull();
    expect(result!.change).toBeCloseTo(-0.01, 2);
    expect(result!.isUp).toBe(false);
  });
});
