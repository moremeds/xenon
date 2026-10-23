import { test } from "node:test";
import assert from "node:assert/strict";
import { createFutuRouting, FUTU_HOLD_DOWN_MS } from "../futu_routing.js";

// Futu provider routing — pure state machine. `now` is always caller-passed.
// Covers: client preference (opted-in API-key vs browser/un-opted), the want
// set, assign partitions, the 60 s hold-down, per-source failover, and
// IB-demand recomputation (the "IB line count drops" rule).

const HD = 1_000; // small hold-down for tests

function setup() {
  return createFutuRouting({ holdDownMs: HD, healthDeadlineMs: 15_000 });
}

const futuClientA = { name: "argon" };
const apiKeyClientB = { name: "apex" }; // api-key but NOT opted in
const browserClient = { name: "browser" };

function liveFutu(r, okSymbols, now = 0) {
  r.noteSocket(true);
  r.noteHealth({ opend_connected: true, quota: { ceiling: 800 } }, now);
  r.applyAssign({ gen: 1, ok: okSymbols, pending: [], rejected: [] }, now);
}

test("want set = futu-first clients' symbols minus IB-pinned", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  r.setPref(browserClient, "ib");
  const clientSymbols = new Map([
    [futuClientA, new Set(["AAPL", "VIX", "SPY_20261218_500_C"])],
    [browserClient, new Set(["MSFT"])],
  ]);
  r.pinIbOnly("VIX");
  r.pinIbOnly("SPY_20261218_500_C");
  assert.deepEqual(r.desiredWant(clientSymbols), ["AAPL"]);
});

test("routeFor: only opted-in client gets futu when futu-live", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  r.setPref(apiKeyClientB, "ib");
  r.setPref(browserClient, "ib");
  liveFutu(r, ["AAPL"], 0);
  const t = HD + 1;
  assert.equal(r.routeFor(futuClientA, "AAPL", t), "futu");
  assert.equal(r.routeFor(apiKeyClientB, "AAPL", t), "ib");
  assert.equal(r.routeFor(browserClient, "AAPL", t), "ib");
});

test("a symbol can be live on both sources (shared symbol)", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, ["AAPL"], 0);
  const t = HD + 1;
  const subs = new Set([futuClientA, browserClient]);
  // futuLive true for the futu-first client AND IB demand stays true for the
  // browser subscriber — both sources live at once.
  assert.equal(r.futuLive("AAPL", t), true);
  assert.equal(r.ibDemand("AAPL", subs, t), true);
});

test("hold-down: not routed to futu until ok is stable for 60s", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, ["AAPL"], 0);
  assert.equal(r.futuLive("AAPL", HD - 1), false);
  assert.equal(r.futuLive("AAPL", HD), true);
});

test("IB demand drops for futu-served symbols (line freed)", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, ["AAPL"], 0);
  const subs = new Set([futuClientA]);
  assert.equal(r.ibDemand("AAPL", subs, 0), true); // hold-down: IB still needed
  assert.equal(r.ibDemand("AAPL", subs, HD + 1), false); // futu-live: no IB line
});

test("quota rejection keeps symbol on IB", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, [], 0); // up, but nothing in ok
  r.applyAssign(
    {
      gen: 2,
      ok: [],
      pending: [],
      rejected: [{ symbol: "HYG", reason: "quota" }],
    },
    10,
  );
  const subs = new Set([futuClientA]);
  assert.equal(r.futuLive("HYG", HD + 100), false);
  assert.equal(r.ibDemand("HYG", subs, HD + 100), true);
});

test("OpenD loss / socket loss fails over to IB immediately", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, ["AAPL"], 0);
  const subs = new Set([futuClientA]);
  const t = HD + 1;
  assert.equal(r.futuLive("AAPL", t), true);
  assert.equal(r.ibDemand("AAPL", subs, t), false);
  // health frame with opend_connected:false
  r.noteHealth({ opend_connected: false, quota: null }, t + 10);
  assert.equal(r.futuLive("AAPL", t + 10), false);
  assert.equal(r.ibDemand("AAPL", subs, t + 10), true);
  // socket loss path
  r.noteHealth({ opend_connected: true, quota: {} }, t + 20);
  r.applyAssign({ gen: 2, ok: ["AAPL"], pending: [], rejected: [] }, t + 20);
  assert.equal(r.futuLive("AAPL", t + 20 + HD + 1), true);
  r.noteSocket(false);
  assert.equal(r.futuLive("AAPL", t + 20 + HD + 1), false);
});

test("health deadline marks futu down (stalled service)", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, ["AAPL"], 0);
  r.noteDown("health_deadline", 100);
  assert.equal(r.isUp(), false);
  assert.equal(r.futuLive("AAPL", 100 + HD + 1), false);
});

test("recovery: fresh health + new ok + full hold-down", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, ["AAPL"], 0);
  r.noteDown("socket_close", 50);
  assert.equal(r.futuLive("AAPL", 2000), false);
  // Recovery at t=1000: ok at 1000, live only at 1000+HD
  r.noteSocket(true);
  r.noteHealth({ opend_connected: true, quota: {} }, 1000);
  r.applyAssign({ gen: 1, ok: ["AAPL"], pending: [], rejected: [] }, 1000);
  assert.equal(r.futuLive("AAPL", 1000 + HD - 1), false);
  assert.equal(r.futuLive("AAPL", 1000 + HD), true);
});

test("symbol leaving ok mid-epoch needs a fresh hold-down to return", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, ["AAPL"], 0);
  r.applyAssign({ gen: 2, ok: [], pending: [], rejected: [] }, HD + 1);
  assert.equal(r.futuLive("AAPL", HD + 2), false);
  r.applyAssign({ gen: 3, ok: ["AAPL"], pending: [], rejected: [] }, HD + 5);
  assert.equal(r.futuLive("AAPL", HD + 5 + HD - 1), false);
  assert.equal(r.futuLive("AAPL", HD + 5 + HD), true);
});

test("indexes stay on IB (pinned symbol never futu-live)", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  r.pinIbOnly("SPX");
  liveFutu(r, ["SPX"], 0); // even if the service claims ok
  const subs = new Set([futuClientA]);
  assert.equal(r.futuLive("SPX", HD + 1), false);
  assert.equal(r.routeFor(futuClientA, "SPX", HD + 1), "ib");
  assert.equal(r.ibDemand("SPX", subs, HD + 1), true);
});

test("stale gen assign is ignored", () => {
  const r = setup();
  liveFutu(r, ["AAPL"], 0);
  r.applyAssign({ gen: 1, ok: [], pending: [], rejected: [] }, 10); // same gen
  assert.equal(r.futuLive("AAPL", HD + 1), true); // unchanged
  r.applyAssign({ gen: 0, ok: [], pending: [], rejected: [] }, 11); // lower gen
  assert.equal(r.futuLive("AAPL", HD + 1), true);
});

test("down clears futu tick cache", () => {
  const r = setup();
  liveFutu(r, ["AAPL"], 0);
  r.recordTick("AAPL", { last: 1 });
  r.noteDown("socket_close", 10);
  assert.equal(r.futuData("AAPL"), undefined);
});

test("status reports connected/opend_connected/quota", () => {
  const r = setup();
  assert.deepEqual(r.status(), {
    connected: false,
    opend_connected: false,
    quota: null,
  });
  r.noteSocket(true);
  r.noteHealth({ opend_connected: true, quota: { remain: 800 } }, 5);
  assert.deepEqual(r.status(), {
    connected: true,
    opend_connected: true,
    quota: { remain: 800 },
  });
  r.noteDown("health_deadline", 10);
  assert.deepEqual(r.status(), {
    connected: true,
    opend_connected: false,
    quota: null,
  });
});

test("non-subscribed symbol has no IB demand", () => {
  const r = setup();
  assert.equal(r.ibDemand("ZZZZ", new Set(), 0), false);
});

test("hold-down default is 60s", () => {
  assert.equal(FUTU_HOLD_DOWN_MS, 60_000);
});
