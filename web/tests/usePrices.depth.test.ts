import { describe, it, expect } from "vitest";
import { applyDepthMessage } from "@/lib/usePrices";
import type { DepthBook } from "@/lib/pricesProtocol";

const book = (s: string): DepthBook => ({
  symbol: s,
  kind: "stock",
  bid: [],
  ask: [],
  isSmartDepth: true,
  feed: "SMART",
  entitled: true,
  timestamp: "t",
});

describe("usePrices depth message handling", () => {
  it("depth-batch merges by symbol", () => {
    const s = applyDepthMessage(
      { depths: {}, tape: {} },
      { type: "depth-batch", updates: { QQQ: book("QQQ") } },
    );
    expect(s.depths.QQQ.entitled).toBe(true);
  });

  it("depth-unavailable(no-entitlement) writes an unentitled shell", () => {
    const s = applyDepthMessage(
      { depths: {}, tape: {} },
      { type: "depth-unavailable", symbol: "QQQ", reason: "no-entitlement" },
    );
    expect(s.depths.QQQ.entitled).toBe(false);
  });

  it("depth-unavailable(recycled) drops the stale book", () => {
    const s = applyDepthMessage(
      { depths: { QQQ: book("QQQ") }, tape: {} },
      { type: "depth-unavailable", symbol: "QQQ", reason: "recycled" },
    );
    expect(s.depths.QQQ).toBeUndefined();
  });

  it("tape-batch replaces (newest-last, bounded)", () => {
    const s = applyDepthMessage(
      { depths: {}, tape: {} },
      {
        type: "tape-batch",
        updates: { QQQ: [{ price: 1, size: 1, exchange: null, time: "1" }] },
      },
    );
    expect(s.tape.QQQ.length).toBe(1);
  });
});
