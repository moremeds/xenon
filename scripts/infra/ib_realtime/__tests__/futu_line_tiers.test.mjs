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

test("pickEvictable takes the idlest view line first, never held for held", () => {
  const entries = [
    { key: "V_OLD", tickerId: 1, lastAccessAt: 10, tier: "view" },
    { key: "V_NEW", tickerId: 2, lastAccessAt: 99, tier: "view" },
    { key: "H_OLD", tickerId: 3, lastAccessAt: 1, tier: "held" },
  ];
  assert.equal(pickEvictable(entries), "V_OLD");
  // With no view line left, a held incoming never evicts another held line.
  assert.equal(
    pickEvictable(
      [{ key: "H_OLD", tickerId: 3, lastAccessAt: 1, tier: "held" }],
      null,
      "held",
    ),
    null,
  );
  // …but view demand may still take the idlest held line as a last resort
  // (pre-tier LRU fallback).
  assert.equal(
    pickEvictable(
      [{ key: "H_OLD", tickerId: 3, lastAccessAt: 1, tier: "held" }],
      null,
      "view",
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
