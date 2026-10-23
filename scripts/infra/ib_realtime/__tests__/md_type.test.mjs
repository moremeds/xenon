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

test("reset clears mdType on resubscribe/reconnect", () => {
  const state = { tickerId: 1, mdType: 3 };
  resetMdType(state);
  assert.equal(state.mdType, null);
});
