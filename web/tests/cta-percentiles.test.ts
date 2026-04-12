import { describe, it, expect } from "vitest";
import {
  normalizeCtaPercentile,
  formatCtaPercentileLabel,
} from "../lib/ctaPercentiles";

describe("normalizeCtaPercentile", () => {
  it("passes through 0-100 values unchanged", () => {
    expect(normalizeCtaPercentile(50)).toBe(50);
    expect(normalizeCtaPercentile(0)).toBe(0);
    expect(normalizeCtaPercentile(100)).toBe(100);
  });

  it("scales 0-1 fractional values to 0-100", () => {
    expect(normalizeCtaPercentile(0.5)).toBe(50);
    expect(normalizeCtaPercentile(0.95)).toBe(95);
  });

  it("clamps values above 100", () => {
    expect(normalizeCtaPercentile(150)).toBe(100);
  });

  it("clamps negative values to 0", () => {
    expect(normalizeCtaPercentile(-10)).toBe(0);
  });

  it("returns null for null/undefined/NaN", () => {
    expect(normalizeCtaPercentile(null)).toBe(null);
    expect(normalizeCtaPercentile(undefined)).toBe(null);
    expect(normalizeCtaPercentile(NaN)).toBe(null);
  });
});

describe("formatCtaPercentileLabel", () => {
  it("formats ordinal suffixes correctly", () => {
    // Note: values 0-1 are treated as fractional (0-100%), so 1 → 100th
    expect(formatCtaPercentileLabel(1)).toBe("100th");
    expect(formatCtaPercentileLabel(2)).toBe("2nd");
    expect(formatCtaPercentileLabel(3)).toBe("3rd");
    expect(formatCtaPercentileLabel(4)).toBe("4th");
    expect(formatCtaPercentileLabel(11)).toBe("11th");
    expect(formatCtaPercentileLabel(12)).toBe("12th");
    expect(formatCtaPercentileLabel(13)).toBe("13th");
    expect(formatCtaPercentileLabel(21)).toBe("21st");
    expect(formatCtaPercentileLabel(22)).toBe("22nd");
    expect(formatCtaPercentileLabel(23)).toBe("23rd");
    expect(formatCtaPercentileLabel(99)).toBe("99th");
  });

  it("returns --- for null input", () => {
    expect(formatCtaPercentileLabel(null)).toBe("---");
  });

  it("handles fractional input (0-1 range)", () => {
    expect(formatCtaPercentileLabel(0.75)).toBe("75th");
  });

  it("formats 1st percentile from fractional 0.01", () => {
    expect(formatCtaPercentileLabel(0.01)).toBe("1st");
  });
});
