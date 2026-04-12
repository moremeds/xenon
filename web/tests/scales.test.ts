import { describe, it, expect } from "vitest";
import { scaleLinear, scaleTime } from "../lib/scales";

describe("scaleLinear", () => {
  it("maps domain to range linearly", () => {
    const s = scaleLinear().domain([0, 100]).range([0, 500]);
    expect(s(0)).toBe(0);
    expect(s(50)).toBe(250);
    expect(s(100)).toBe(500);
  });

  it("handles inverted domain", () => {
    const s = scaleLinear().domain([100, 0]).range([0, 500]);
    expect(s(100)).toBe(0);
    expect(s(0)).toBe(500);
  });

  it("invert maps range back to domain", () => {
    const s = scaleLinear().domain([0, 100]).range([0, 500]);
    expect(s.invert(250)).toBe(50);
    expect(s.invert(0)).toBe(0);
  });

  it("handles zero-width domain (returns midpoint)", () => {
    const s = scaleLinear().domain([50, 50]).range([0, 100]);
    expect(s(50)).toBe(50); // (r0 + r1) / 2
  });

  it("generates nice ticks", () => {
    const s = scaleLinear().domain([0, 100]);
    const ticks = s.ticks(5);
    expect(ticks.length).toBeGreaterThan(0);
    expect(ticks[0]).toBeGreaterThanOrEqual(0);
    expect(ticks[ticks.length - 1]).toBeLessThanOrEqual(100);
    // Ticks should be evenly spaced
    const step = ticks[1] - ticks[0];
    for (let i = 2; i < ticks.length; i++) {
      expect(ticks[i] - ticks[i - 1]).toBeCloseTo(step, 10);
    }
  });

  it("returns empty ticks for invalid inputs", () => {
    const s = scaleLinear().domain([NaN, 100]);
    expect(s.ticks(5)).toEqual([]);
  });
});

describe("scaleTime", () => {
  it("maps Date domain to numeric range", () => {
    const d0 = new Date("2026-01-01T00:00:00Z");
    const d1 = new Date("2026-01-02T00:00:00Z");
    const s = scaleTime().domain([d0, d1]).range([0, 100]);
    const midpoint = new Date("2026-01-01T12:00:00Z");
    expect(s(midpoint)).toBeCloseTo(50, 0);
  });

  it("invert maps back to Date", () => {
    const d0 = new Date("2026-01-01T00:00:00Z");
    const d1 = new Date("2026-01-02T00:00:00Z");
    const s = scaleTime().domain([d0, d1]).range([0, 100]);
    const result = s.invert(50);
    expect(result.getTime()).toBeCloseTo(
      new Date("2026-01-01T12:00:00Z").getTime(),
      -3,
    );
  });

  it("generates time ticks", () => {
    const d0 = new Date("2026-01-01T00:00:00Z");
    const d1 = new Date("2026-01-02T00:00:00Z");
    const s = scaleTime().domain([d0, d1]).range([0, 100]);
    const ticks = s.ticks(6);
    expect(ticks.length).toBeGreaterThan(0);
    expect(ticks[0].getTime()).toBeGreaterThanOrEqual(d0.getTime());
  });

  it("returns single tick for zero-width domain", () => {
    const d = new Date("2026-01-01T00:00:00Z");
    const s = scaleTime().domain([d, d]).range([0, 100]);
    expect(s.ticks(5)).toHaveLength(1);
  });
});
