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
  // Untiered entries count as held, so the LRU cases below pass
  // allowHeld=true explicitly — the incoming-held subscribe the classic
  // pre-tier behavior models.
  it("returns the oldest-access live line", () => {
    const entries = [
      { key: "AAPL", tickerId: 1, lastAccessAt: 300 },
      { key: "FAR_OTM", tickerId: 2, lastAccessAt: 100 }, // idlest
      { key: "TSLA", tickerId: 3, lastAccessAt: 200 },
    ];
    expect(pickEvictable(entries, null, true)).toBe("FAR_OTM");
  });

  it("never evicts the incoming symbol even if it is idlest", () => {
    const entries = [
      { key: "NEW", tickerId: 9, lastAccessAt: 1 }, // idlest but excluded
      { key: "AAPL", tickerId: 1, lastAccessAt: 300 },
    ];
    expect(pickEvictable(entries, "NEW", true)).toBe("AAPL");
  });

  it("skips lines that hold no ticket (already evicted)", () => {
    const entries = [
      { key: "EVICTED", tickerId: null, lastAccessAt: 1 },
      { key: "AAPL", tickerId: 1, lastAccessAt: 300 },
    ];
    expect(pickEvictable(entries, null, true)).toBe("AAPL");
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
    expect(pickEvictable(entries, null, true)).toBe("STALE");
  });

  it("evicts the idlest view line first; held victims need allowHeld", () => {
    const entries = [
      { key: "HELD_IDLE", tickerId: 1, lastAccessAt: 1 },
      { key: "VIEW", tickerId: 2, lastAccessAt: 999, tier: "view" },
    ];
    // View lines leave first even though the held line is idler.
    expect(pickEvictable(entries)).toBe("VIEW");
    const heldOnly = [
      { key: "HELD_A", tickerId: 1, lastAccessAt: 1 },
      { key: "HELD_B", tickerId: 2, lastAccessAt: 2 },
    ];
    // View demand / the pending-held reclaim never displace a held line.
    expect(pickEvictable(heldOnly)).toBe(null);
    // A held subscribe keeps the pre-tier LRU on a full held budget.
    expect(pickEvictable(heldOnly, null, true)).toBe("HELD_A");
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
