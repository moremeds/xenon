import { describe, it, expect } from "vitest";
import {
  applyDepthDelta,
  serializeLadder,
  summarizeOptionNbbo,
} from "../../../scripts/infra/ib_realtime/depth_book.js";

const newLadders = () => ({ bid: [], ask: [] });

describe("depth_book.applyDepthDelta", () => {
  it("insert (op=0) splices a level at position on the correct side", () => {
    const L = newLadders();
    applyDepthDelta(L, 0, "MM1", 0, 1, 100.5, 300); // side=1 → bid
    expect(L.bid).toEqual([{ price: 100.5, size: 300, marketMaker: "MM1" }]);
    expect(L.ask).toEqual([]);
  });
  it("update (op=1) replaces level at position", () => {
    const L = newLadders();
    applyDepthDelta(L, 0, "MM1", 0, 0, 101.0, 200); // ask insert
    applyDepthDelta(L, 0, "MM1", 1, 0, 101.0, 250); // ask update
    expect(L.ask[0].size).toBe(250);
  });
  it("delete (op=2) removes level at position", () => {
    const L = newLadders();
    applyDepthDelta(L, 0, "MM1", 0, 1, 100.5, 300);
    applyDepthDelta(L, 0, "MM1", 2, 1, 100.5, 0);
    expect(L.bid).toEqual([]);
  });
  it("update past end is OOB-defensive (inserts)", () => {
    const L = newLadders();
    applyDepthDelta(L, 5, "MM1", 1, 1, 99.0, 100); // update at empty pos 5
    expect(L.bid.length).toBe(1);
  });
  it("delete past end is ignored (no throw, no change)", () => {
    const L = newLadders();
    applyDepthDelta(L, 0, "MM1", 0, 1, 100.5, 300);
    applyDepthDelta(L, 9, "MM1", 2, 1, 0, 0); // delete OOB
    expect(L.bid.length).toBe(1);
  });
  it("normalizes a missing marketMaker to null (stock single-venue path)", () => {
    const L = newLadders();
    applyDepthDelta(L, 0, undefined, 0, 0, 50.0, 12);
    expect(L.ask[0].marketMaker).toBeNull();
  });
});

describe("depth_book.serializeLadder", () => {
  it("maps internal rows to DepthLevel[] for a stock (MPID exposed as exchange)", () => {
    const rows = [{ price: 100.5, size: 300, marketMaker: "ARCA" }];
    // radon source (:1285-1288): equities expose the MPID as `exchange` and keep
    // marketMaker null; the montage Market column reads `marketMaker ?? exchange`.
    expect(serializeLadder(rows, false, "stock", "bid")).toEqual([
      { price: 100.5, size: 300, marketMaker: null, exchange: "ARCA" },
    ]);
  });
  it("populates both marketMaker and exchange + flags inside venue for an option ladder", () => {
    const bidRows = [
      { price: 1.5, size: 5, marketMaker: "CBOE" },
      { price: 1.25, size: 9, marketMaker: "PHLX" },
    ];
    const out = serializeLadder(bidRows, false, "option", "bid");
    expect(out[0]).toEqual({
      price: 1.5,
      size: 5,
      marketMaker: "CBOE",
      exchange: "CBOE",
      nbbo: true, // inside bid is the max price across venues
    });
    expect(out[1].nbbo).toBe(false);
  });
  it("flags the min price as the inside on an option ask ladder", () => {
    const askRows = [
      { price: 2.5, size: 4, marketMaker: "AMEX" },
      { price: 2.0, size: 3, marketMaker: "CBOE" },
    ];
    const out = serializeLadder(askRows, false, "option", "ask");
    expect(out[0].nbbo).toBe(false);
    expect(out[1].nbbo).toBe(true); // inside ask is the min price
  });
  it("futures rows carry no venue attribution (single-venue native depth)", () => {
    const rows = [{ price: 5000.25, size: 12, marketMaker: "CME" }];
    expect(serializeLadder(rows, true, "future", "ask")).toEqual([
      { price: 5000.25, size: 12, marketMaker: null, exchange: null },
    ]);
  });
});

describe("depth_book.summarizeOptionNbbo", () => {
  it("computes best bid/ask, mid, and summed inside sizes", () => {
    const bid = [
      { price: 1.5, size: 5 },
      { price: 1.25, size: 9 },
    ];
    const ask = [
      { price: 2.0, size: 3 },
      { price: 2.5, size: 4 },
    ];
    expect(summarizeOptionNbbo(bid, ask)).toEqual({
      bestBid: 1.5,
      bestAsk: 2.0,
      mid: 1.75,
      bidSize: 14,
      askSize: 7,
    });
  });
  it("returns null prices/mid when a side is empty", () => {
    expect(summarizeOptionNbbo([], [])).toEqual({
      bestBid: null,
      bestAsk: null,
      mid: null,
      bidSize: 0,
      askSize: 0,
    });
  });
});
