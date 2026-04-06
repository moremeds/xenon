import { describe, it, expect } from "vitest";
import { isSkewCacheFresh } from "@/lib/internalsSkewCache";

describe("isSkewCacheFresh", () => {
  it("returns true when latest date is today", () => {
    expect(isSkewCacheFresh("2026-03-20", "2026-03-20")).toBe(true);
  });

  it("returns true when latest date is yesterday (overnight/pre-market)", () => {
    expect(isSkewCacheFresh("2026-03-19", "2026-03-20")).toBe(true);
  });

  it("returns false when latest date is 2 days behind (stale)", () => {
    // e.g., cache has Wednesday data, today is Friday
    expect(isSkewCacheFresh("2026-03-18", "2026-03-20")).toBe(false);
  });

  it("returns false when latest date is 3+ days behind", () => {
    expect(isSkewCacheFresh("2026-03-17", "2026-03-20")).toBe(false);
  });

  it("returns false for weekend gap — Friday cache on Monday", () => {
    // Friday March 20 cache, Monday March 23
    expect(isSkewCacheFresh("2026-03-20", "2026-03-23")).toBe(false);
  });

  it("returns true for Friday cache on Saturday (next calendar day)", () => {
    expect(isSkewCacheFresh("2026-03-20", "2026-03-21")).toBe(true);
  });

  it("returns false for Friday cache on Sunday (2 calendar days)", () => {
    expect(isSkewCacheFresh("2026-03-20", "2026-03-22")).toBe(false);
  });

  it("returns false for invalid latestDate", () => {
    expect(isSkewCacheFresh("not-a-date", "2026-03-20")).toBe(false);
  });

  it("returns false for invalid todayET", () => {
    expect(isSkewCacheFresh("2026-03-20", "bad")).toBe(false);
  });

  it("returns false for empty string", () => {
    expect(isSkewCacheFresh("", "2026-03-20")).toBe(false);
  });

  it("accepts custom maxStaleDays", () => {
    // 3 days behind, but maxStaleDays=3 allows it
    expect(isSkewCacheFresh("2026-03-17", "2026-03-20", 3)).toBe(true);
    // 4 days behind, maxStaleDays=3 rejects
    expect(isSkewCacheFresh("2026-03-16", "2026-03-20", 3)).toBe(false);
  });
});
