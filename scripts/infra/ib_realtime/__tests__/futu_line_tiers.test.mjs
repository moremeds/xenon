import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import path from "node:path";
import {
  pickAdmittable,
  pickEvictable,
} from "../line_budget.js";

// D13 held-first IB line allocation (PR #250 review F1):
//   - lineEntries carries a merged subscriber tier (any held → held; absent
//     tier map → default held; all-view → view);
//   - pickAdmittable admits held pending before view (recency within tier);
//   - pickEvictable evicts view lines before held (idlest within tier), and a
//     held key never evicts another held key for held-first reasons;
//   - fillFreeLines/admitPendingLine force-evict a view line when a held key
//     is pending over a view-only budget;
//   - restoreSubscriptions restores held keys before view keys (recency
//     within tier).
//
// The allocation functions live inside the relay process, so the fill and
// restore paths are exercised by extracting their source into a VM context
// with stubbed broker actions — the same technique the PR #250 review used
// to reproduce the bug.

const SERVER = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "ib_realtime_server.js",
);
const source = fs.readFileSync(SERVER, "utf8");

function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf("\n}", start) + 2;
  assert(start >= 0 && end > start, `extract ${name}`);
  return source.slice(start, end);
}

function makeCtx({ budget, viewLive = [], viewPending = [], heldPending = [] }) {
  const subscriberTiers = new Map();
  const symbolSubscribers = new Map();
  const symbolStates = new Map();
  const calls = [];
  const evictions = [];
  const add = (key, tier, tickerId, lastAccessAt) => {
    subscriberTiers.set(key, new Map([["c", tier]]));
    symbolSubscribers.set(key, new Set(["c"]));
    symbolStates.set(key, { contract: {}, tickerId, lastAccessAt });
  };
  viewLive.forEach(([key, at]) => add(key, "view", 1, at));
  viewPending.forEach(([key, at]) => add(key, "view", null, at));
  heldPending.forEach(([key, at]) => add(key, "held", null, at));
  const ctx = {
    subscriberTiers,
    symbolSubscribers,
    symbolStates,
    pickAdmittable,
    pickEvictable,
    MAX_CONCURRENT_LINES: budget,
    ibConnected: true,
    futuRouting: { ibDemand: () => true },
    startLiveSubscription(key) {
      calls.push(key);
      symbolStates.get(key).tickerId = calls.length;
    },
    evictLine(key) {
      evictions.push(key);
      symbolStates.get(key).tickerId = null;
    },
    sendToSymbolSubscribers() {},
    withStale: (s) => s,
    Date,
  };
  vm.createContext(ctx);
  const defs = [
    "activeLineCount",
    "lineEntries",
    "demandedLineEntries",
    "admitPendingLine",
    "fillFreeLines",
    "restoreSubscriptions",
  ]
    .map(extract)
    .join("\n");
  return { ctx, defs, calls, evictions };
}

test("pickAdmittable admits held pending before newer view pending", () => {
  const entries = [
    { key: "V9", tickerId: null, lastAccessAt: 900, tier: "view" },
    { key: "H1", tickerId: null, lastAccessAt: 1, tier: "held" },
    { key: "V8", tickerId: null, lastAccessAt: 800, tier: "view" },
  ];
  assert.equal(pickAdmittable(entries), "H1");
  // Within a tier, recency wins.
  assert.equal(
    pickAdmittable([
      { key: "V1", tickerId: null, lastAccessAt: 5, tier: "view" },
      { key: "V2", tickerId: null, lastAccessAt: 50, tier: "view" },
    ]),
    "V2",
  );
  // Untiered entries are held by default.
  assert.equal(
    pickAdmittable([
      { key: "V1", tickerId: null, lastAccessAt: 99, tier: "view" },
      { key: "OLD", tickerId: null, lastAccessAt: 1 },
    ]),
    "OLD",
  );
});

test("pickEvictable takes the idlest view line first; held only when allowed", () => {
  const entries = [
    { key: "V_OLD", tickerId: 1, lastAccessAt: 10, tier: "view" },
    { key: "V_NEW", tickerId: 2, lastAccessAt: 99, tier: "view" },
    { key: "H_OLD", tickerId: 3, lastAccessAt: 1, tier: "held" },
  ];
  assert.equal(pickEvictable(entries), "V_OLD");
  // With no view line left: allowHeld=false (incoming view, or the pending-held
  // reclaim) gets nothing — view lines never displace held.
  assert.equal(
    pickEvictable(
      [{ key: "H_OLD", tickerId: 3, lastAccessAt: 1, tier: "held" }],
      null,
      false,
    ),
    null,
  );
  // …while allowHeld=true (incoming held subscribe) keeps the pre-tier LRU:
  // the idlest held line is evictable.
  assert.equal(
    pickEvictable(
      [{ key: "H_OLD", tickerId: 3, lastAccessAt: 1, tier: "held" }],
      null,
      true,
    ),
    "H_OLD",
  );
  // exceptKey is never evicted.
  assert.equal(
    pickEvictable(
      [{ key: "V", tickerId: 1, lastAccessAt: 1, tier: "view" }],
      "V",
    ),
    null,
  );
});

test("fill path: over-budget pending held evicts a view line and is served", () => {
  const { ctx, defs, calls, evictions } = makeCtx({
    budget: 10,
    viewLive: Array.from({ length: 10 }, (_, i) => [`SYNVIEW${i}`, 100 + i]),
    heldPending: [["SYNHELD", 1]],
  });
  vm.runInContext(`${defs}\nfillFreeLines();`, ctx);
  assert.ok(
    evictions.some((k) => k.startsWith("SYNVIEW")),
    "a view line should be evicted for the pending held key",
  );
  assert.ok(calls.includes("SYNHELD"), "held key must be admitted");
  assert.equal(
    evictions.filter((k) => k === "SYNHELD").length,
    0,
    "held key never evicts held",
  );
});

test("fill path: held pending is admitted before newer view pending", () => {
  const { ctx, defs, calls } = makeCtx({
    budget: 10,
    viewPending: Array.from({ length: 10 }, (_, i) => [
      `SYNVIEW${i}`,
      100 + i,
    ]),
    heldPending: [["SYNHELD", 1]],
  });
  vm.runInContext(`${defs}\nfillFreeLines();`, ctx);
  assert.equal(calls[0], "SYNHELD", "held admitted before any view");
  assert.ok(calls.includes("SYNHELD"));
});

test("restore path: held keys restore before newer view keys", () => {
  const { ctx, defs, calls } = makeCtx({
    budget: 10,
    viewPending: Array.from({ length: 10 }, (_, i) => [
      `SYNVIEW${i}`,
      100 + i,
    ]),
    heldPending: [["SYNHELD", 1]],
  });
  vm.runInContext(`${defs}\nrestoreSubscriptions();`, ctx);
  assert.equal(calls[0], "SYNHELD", "held restored before any view");
  assert.ok(calls.includes("SYNHELD"), "held must be restored");
});

// The subscribe path runs the real makeRoomForLine + startLiveSubscription,
// with the broker calls stubbed: evictLine just drops the line (records the
// victim), ib.reqMktData is a no-op (admission is observable via
// state.tickerId), ensureSymbolState is a minimal in-memory version.
function makeSubscribeCtx({ budget, heldLive = [], incoming = [] }) {
  const subscriberTiers = new Map();
  const symbolSubscribers = new Map();
  const symbolStates = new Map();
  const evictions = [];
  const add = (key, tier, tickerId, lastAccessAt) => {
    subscriberTiers.set(key, new Map([["c", tier]]));
    symbolSubscribers.set(key, new Set(["c"]));
    symbolStates.set(key, { contract: {}, tickerId, lastAccessAt });
  };
  heldLive.forEach(([key, at], i) => add(key, "held", i + 1, at));
  incoming.forEach(([key, tier]) => add(key, tier, null, 0));
  const ctx = {
    subscriberTiers,
    symbolSubscribers,
    symbolStates,
    pickAdmittable,
    pickEvictable,
    MAX_CONCURRENT_LINES: budget,
    ibConnected: true,
    futuRouting: { ibDemand: () => true },
    evictLine(key) {
      evictions.push(key);
      symbolStates.get(key).tickerId = null;
    },
    ensureSymbolState(key, contract) {
      let s = symbolStates.get(key);
      if (!s) {
        s = { contract, tickerId: null, data: {}, lastAccessAt: 0 };
        symbolStates.set(key, s);
      }
      return s;
    },
    ib: { reqMktData() {}, cancelMktData() {} },
    nextRequestId: 0,
    requestIdToSymbol: new Map(),
    requestFundamentals() {},
    resetMdType() {},
    sendToSymbolSubscribers() {},
    withStale: (s) => s,
    Date,
  };
  vm.createContext(ctx);
  const defs = [
    "activeLineCount",
    "lineEntries",
    "demandedLineEntries",
    "admitPendingLine",
    "fillFreeLines",
    "makeRoomForLine",
    "startLiveSubscription",
  ]
    .map(extract)
    .join("\n");
  return { ctx, defs, evictions };
}

test("subscribe path: incoming held on a full held budget evicts idlest held", () => {
  const { ctx, defs, evictions } = makeSubscribeCtx({
    budget: 10,
    heldLive: Array.from({ length: 10 }, (_, i) => [`SYNHELD${i}`, 10 + i]),
    incoming: [["SYNHELD_NEW", "held"]],
  });
  vm.runInContext(`${defs}\nstartLiveSubscription("SYNHELD_NEW", {});`, ctx);
  assert.deepEqual(
    evictions,
    ["SYNHELD0"],
    "the idlest held line leaves for a new held subscribe (pre-tier LRU)",
  );
  assert.notEqual(
    ctx.symbolStates.get("SYNHELD_NEW").tickerId,
    null,
    "new held symbol is live",
  );
  assert.equal(vm.runInContext("activeLineCount()", ctx), 10);
});

test("subscribe path: incoming view on a full held budget gets no line", () => {
  const { ctx, defs, evictions } = makeSubscribeCtx({
    budget: 10,
    heldLive: Array.from({ length: 10 }, (_, i) => [`SYNHELD${i}`, 10 + i]),
    incoming: [["SYNVIEW_NEW", "view"]],
  });
  vm.runInContext(`${defs}\nstartLiveSubscription("SYNVIEW_NEW", {});`, ctx);
  assert.deepEqual(evictions, [], "view never evicts a held line");
  assert.equal(
    ctx.symbolStates.get("SYNVIEW_NEW").tickerId,
    null,
    "view symbol stays wanted-but-evicted, not live",
  );
  assert.equal(
    vm.runInContext("activeLineCount()", ctx),
    10,
    "line count stays at budget — no over-subscribe",
  );
});
