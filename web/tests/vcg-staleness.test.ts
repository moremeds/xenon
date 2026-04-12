import { describe, it, expect } from "vitest";
import { isVcgDataStale, type VcgDataShape } from "../lib/vcgStaleness";

describe("isVcgDataStale", () => {
  it("returns true when scan_time is missing", () => {
    expect(isVcgDataStale({}, "2026-04-08", false)).toBe(true);
  });

  it("returns true when scan_time is unparseable", () => {
    expect(
      isVcgDataStale({ scan_time: "not-a-date" }, "2026-04-08", false),
    ).toBe(true);
  });

  it("returns true when session date differs from today", () => {
    expect(
      isVcgDataStale(
        { scan_time: "2026-04-07T15:00:00-04:00" },
        "2026-04-08",
        false,
      ),
    ).toBe(true);
  });

  it("returns false when same day + market closed (EOD data final)", () => {
    expect(
      isVcgDataStale(
        { scan_time: "2026-04-08T15:59:00-04:00" },
        "2026-04-08",
        false, // market closed
      ),
    ).toBe(false);
  });

  it("returns false when market open + scan_time within TTL (60s)", () => {
    const recent = new Date(Date.now() - 30_000).toISOString(); // 30s ago
    expect(
      isVcgDataStale(
        { scan_time: recent },
        new Date().toLocaleDateString("sv", { timeZone: "America/New_York" }),
        true, // market open
      ),
    ).toBe(false);
  });

  it("returns true when market open + scan_time exceeds TTL", () => {
    const old = new Date(Date.now() - 120_000).toISOString(); // 2min ago
    expect(
      isVcgDataStale(
        { scan_time: old },
        new Date().toLocaleDateString("sv", { timeZone: "America/New_York" }),
        true,
      ),
    ).toBe(true);
  });
});
