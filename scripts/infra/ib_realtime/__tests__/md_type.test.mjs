import { test } from "node:test";
import assert from "node:assert/strict";
import { recordMdType, resetMdType } from "../md_type.js";

// IB reports the effective marketDataType per request via the marketDataType
// callback. The relay records it on the symbol state and exposes it as
// `mdType` in the per-symbol status/snapshot payload. Indexes must reach
// mdType == 1 (live) for R1 acceptance.

function setup() {
  const requestIdToSymbol = new Map();
  const symbolStates = new Map();
  const snapshotRequests = new Map();
  return { requestIdToSymbol, symbolStates, snapshotRequests };
}

test("records mdType on the live symbol state", () => {
  const maps = setup();
  maps.symbolStates.set("SPX", { tickerId: 42, mdType: null });
  maps.requestIdToSymbol.set(42, "SPX");
  const sym = recordMdType(maps, 42, 1);
  assert.equal(sym, "SPX");
  assert.equal(maps.symbolStates.get("SPX").mdType, 1);
});

test("records mdType on a snapshot request's data", () => {
  const maps = setup();
  maps.requestIdToSymbol.set(77, "MSFT");
  maps.snapshotRequests.set(77, { data: { symbol: "MSFT" } });
  recordMdType(maps, 77, 4);
  assert.equal(maps.snapshotRequests.get(77).data.mdType, 4);
});

test("unknown reqId is a no-op", () => {
  const maps = setup();
  assert.equal(recordMdType(maps, 999, 1), null);
});

test("R234-3: snapshot callback does not touch the live request's mdType", () => {
  // One symbol, two concurrent request ids: reqId 10 is the live stream,
  // reqId 11 is a one-shot snapshot. requestIdToSymbol maps BOTH to SPX.
  const maps = setup();
  maps.symbolStates.set("SPX", { tickerId: 10, mdType: null });
  maps.requestIdToSymbol.set(10, "SPX");
  maps.requestIdToSymbol.set(11, "SPX");
  maps.snapshotRequests.set(11, { data: { symbol: "SPX" } });

  // The live request reports first.
  recordMdType(maps, 10, 3);
  assert.equal(maps.symbolStates.get("SPX").mdType, 3);

  // The snapshot's own callback must not manufacture or overwrite live
  // evidence for the same symbol.
  recordMdType(maps, 11, 1);
  assert.equal(maps.symbolStates.get("SPX").mdType, 3);
  assert.equal(maps.snapshotRequests.get(11).data.mdType, 1);
});

test("R234-3: live resubscribe waits for its own callback", () => {
  const maps = setup();
  const state = { tickerId: 10, mdType: 3 };
  maps.symbolStates.set("SPX", state);
  maps.requestIdToSymbol.set(10, "SPX");
  maps.requestIdToSymbol.set(11, "SPX");
  maps.snapshotRequests.set(11, { data: { symbol: "SPX" } });

  // Resubscribe: new tickerId, mdType reset (production path = resetMdType).
  state.tickerId = null;
  resetMdType(state);
  state.tickerId = 12;
  maps.requestIdToSymbol.delete(10);
  maps.requestIdToSymbol.set(12, "SPX");

  // A late callback for the dead request and for the in-flight snapshot must
  // not populate the new live request's mdType.
  recordMdType(maps, 10, 1); // dead reqId — symbol resolves, live id mismatch
  assert.equal(state.mdType, null);
  recordMdType(maps, 11, 4); // snapshot's own callback
  assert.equal(state.mdType, null);
  assert.equal(maps.snapshotRequests.get(11).data.mdType, 4);

  // The live request's own callback reports.
  recordMdType(maps, 12, 1);
  assert.equal(state.mdType, 1);
});

test("reset clears mdType on resubscribe/reconnect", () => {
  const state = { tickerId: 1, mdType: 3 };
  resetMdType(state);
  assert.equal(state.mdType, null);
});
