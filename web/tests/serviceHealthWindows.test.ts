import { describe, it, expect } from "vitest";

import { isWriterStale } from "@/lib/serviceHealthWindows";
import { MarketState } from "@/lib/useMarketHours";

describe("isWriterStale", () => {
  it("poller fresh within RTH window", () => {
    expect(isWriterStale("ib_activity_poller", 60, MarketState.OPEN)).toBe(
      false,
    );
  });
  it("poller stale past RTH window", () => {
    expect(isWriterStale("ib_activity_poller", 600, MarketState.OPEN)).toBe(
      true,
    );
  });
  it("boot-only writer never stale (has a row)", () => {
    expect(isWriterStale("ib_fills_replay", 999999, MarketState.OPEN)).toBe(
      false,
    );
  });
  it("null age (never reported / missing) is stale", () => {
    expect(isWriterStale("ib_activity_poller", null, MarketState.OPEN)).toBe(
      true,
    );
  });
  it("unknown service uses default window", () => {
    expect(isWriterStale("mystery", 1000, MarketState.OPEN)).toBe(true);
  });
});
