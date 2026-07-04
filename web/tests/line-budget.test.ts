/**
 * IB market-data line-budget LRU selection. Evict the idlest LIVE line to admit
 * a new subscribe; re-admit the hottest evicted-but-wanted symbol when a slot
 * frees. Backs the relay's fix for error 101 "Max number of tickers".
 */
import { describe, it, expect } from "vitest";

const modPath = new URL(
  "../../scripts/infra/ib_realtime/line_budget.js",
  import.meta.url,
).pathname;
const { pickEvictable, pickAdmittable } = await import(modPath);

describe("pickEvictable", () => {
  it("returns the oldest-access live line", () => {
    const entries = [
      { key: "AAPL", tickerId: 1, lastAccessAt: 300 },
      { key: "FAR_OTM", tickerId: 2, lastAccessAt: 100 }, // idlest
      { key: "TSLA", tickerId: 3, lastAccessAt: 200 },
    ];
    expect(pickEvictable(entries)).toBe("FAR_OTM");
  });

  it("never evicts the incoming symbol even if it is idlest", () => {
    const entries = [
      { key: "NEW", tickerId: 9, lastAccessAt: 1 }, // idlest but excluded
      { key: "AAPL", tickerId: 1, lastAccessAt: 300 },
    ];
    expect(pickEvictable(entries, "NEW")).toBe("AAPL");
  });

  it("skips lines that hold no ticket (already evicted)", () => {
    const entries = [
      { key: "EVICTED", tickerId: null, lastAccessAt: 1 },
      { key: "AAPL", tickerId: 1, lastAccessAt: 300 },
    ];
    expect(pickEvictable(entries)).toBe("AAPL");
  });

  it("returns null when nothing is live", () => {
    expect(pickEvictable([{ key: "X", tickerId: null, lastAccessAt: 5 }])).toBe(
      null,
    );
    expect(pickEvictable([])).toBe(null);
  });

  it("treats missing lastAccessAt as oldest (0)", () => {
    const entries = [
      { key: "STALE", tickerId: 1 }, // no lastAccessAt → 0 → idlest
      { key: "AAPL", tickerId: 2, lastAccessAt: 300 },
    ];
    expect(pickEvictable(entries)).toBe("STALE");
  });
});

describe("pickAdmittable", () => {
  it("returns the newest-access evicted-but-wanted symbol", () => {
    const entries = [
      { key: "COLD", tickerId: null, lastAccessAt: 100 },
      { key: "HOT", tickerId: null, lastAccessAt: 500 }, // hottest waiting
      { key: "LIVE", tickerId: 1, lastAccessAt: 999 }, // already live, skip
    ];
    expect(pickAdmittable(entries)).toBe("HOT");
  });

  it("returns null when every wanted symbol already holds a line", () => {
    const entries = [{ key: "LIVE", tickerId: 1, lastAccessAt: 10 }];
    expect(pickAdmittable(entries)).toBe(null);
  });
});
