import { describe, it, expect } from "vitest";

import { downPoolRoles, snapshotterTone } from "@/lib/operatorHealth";
import { MarketState } from "@/lib/useMarketHours";

describe("snapshotterTone", () => {
  it.each([
    [12, MarketState.OPEN, "core"],
    [6510, MarketState.OPEN, "fault"],
    [6510, MarketState.EXTENDED, "fault"],
    [6510, MarketState.CLOSED, "neutral"],
    [12, MarketState.CLOSED, "core"],
    [null, MarketState.OPEN, "core"],
  ])("stale=%s market=%s -> %s", (secs, market, tone) => {
    expect(snapshotterTone(secs, market)).toBe(tone);
  });
});

describe("downPoolRoles", () => {
  it("names roles that are disconnected, only when authenticated", () => {
    const pool = {
      sync: { connected: true },
      orders: { connected: true },
      data: { connected: false },
    };
    expect(downPoolRoles("authenticated", pool)).toEqual(["data"]);
    expect(downPoolRoles("unreachable", pool)).toEqual([]);
    expect(downPoolRoles("authenticated", {})).toEqual([]);
  });
});
