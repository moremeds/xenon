import { describe, expect, it } from "vitest";
import { formatEtTime } from "../lib/timeFormat";

describe("formatEtTime", () => {
  it("renders a UTC timestamp in Eastern time with the ET suffix", () => {
    // June = EDT (UTC-4): 19:17:15Z -> 15:17:15 ET
    expect(formatEtTime("2026-06-11T19:17:15+00:00")).toBe("15:17:15 ET");
  });

  it("renders winter timestamps in EST (UTC-5)", () => {
    expect(formatEtTime("2026-01-15T19:17:15+00:00")).toBe("14:17:15 ET");
  });

  it("returns an em dash for unparseable input", () => {
    expect(formatEtTime("not-a-date")).toBe("—");
  });
});
