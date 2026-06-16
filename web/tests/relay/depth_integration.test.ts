import { describe, it, expect } from "vitest";
import {
  applyDepthDelta,
  serializeLadder,
  summarizeOptionNbbo,
} from "../../../scripts/infra/ib_realtime/depth_book.js";

// The relay's WS-emission wiring (subscribe-depth → reqMktDepth → updateMktDepthL2
// → depth-batch) is covered by the Phase-3b live E2E. Here we exercise the pure
// accumulate→serialize path the relay drives on every delta, end to end.

describe("depth backend integration (pure path)", () => {
  it("a sequence of deltas serializes to a sorted DepthBook bid side", () => {
    const L = { bid: [], ask: [] };
    applyDepthDelta(L, 0, "A", 0, 1, 100.5, 300); // bid insert @100.5
    applyDepthDelta(L, 1, "B", 0, 1, 100.4, 500); // bid insert @100.4 below it
    const book = serializeLadder(L.bid, false, "stock", "bid");
    expect(book.map((r) => r.price)).toEqual([100.5, 100.4]);
    expect(book[0]).toMatchObject({
      size: 300,
      exchange: "A",
      marketMaker: null,
    });
  });

  it("an L2 update mutates the live ladder in place before serialize", () => {
    const L = { bid: [], ask: [] };
    applyDepthDelta(L, 0, "ARCA", 0, 0, 101.0, 200); // ask insert
    applyDepthDelta(L, 0, "ARCA", 1, 0, 101.0, 275); // ask update size
    const ask = serializeLadder(L.ask, false, "stock", "ask");
    expect(ask[0]).toMatchObject({ price: 101.0, size: 275, exchange: "ARCA" });
  });

  it("an option montage flags inside venues and summarizes the NBBO", () => {
    const L = { bid: [], ask: [] };
    applyDepthDelta(L, 0, "CBOE", 0, 1, 1.5, 5); // bid CBOE 1.50
    applyDepthDelta(L, 1, "PHLX", 0, 1, 1.25, 9); // bid PHLX 1.25
    applyDepthDelta(L, 0, "AMEX", 0, 0, 2.0, 3); // ask AMEX 2.00
    applyDepthDelta(L, 1, "MIAX", 0, 0, 2.5, 4); // ask MIAX 2.50
    const bid = serializeLadder(L.bid, false, "option", "bid");
    const ask = serializeLadder(L.ask, false, "option", "ask");
    expect(bid[0].nbbo).toBe(true); // 1.50 inside bid
    expect(bid[1].nbbo).toBe(false);
    expect(ask[0].nbbo).toBe(true); // 2.00 inside ask
    expect(summarizeOptionNbbo(bid, ask)).toEqual({
      bestBid: 1.5,
      bestAsk: 2.0,
      mid: 1.75,
      bidSize: 14,
      askSize: 7,
    });
  });
});
