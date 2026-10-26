import { test } from "node:test";
import assert from "node:assert/strict";
import { createFutuRouting, FUTU_HOLD_DOWN_MS } from "../futu_routing.js";

// Futu provider routing — pure state machine. `now` is always caller-passed.
// Covers: client preference (opted-in API-key vs browser/un-opted), the want
// set, assign partitions for the CURRENT want gen (R234-1: same gen repeats,
// every partition change applies), the 60 s hold-down, per-source failover,
// and IB-demand recomputation (the "IB line count drops" rule).

const HD = 1_000; // small hold-down for tests

function setup() {
  return createFutuRouting({ holdDownMs: HD });
}

const futuClientA = { name: "argon" };
const apiKeyClientB = { name: "apex" }; // api-key but NOT opted in
const browserClient = { name: "browser" };

function liveFutu(r, okSymbols, now = 0, wantGen = 1) {
  r.noteSocket(true);
  r.noteHealth({ opend_connected: true, quota: { ceiling: 800 } });
  r.applyAssign(
    { gen: wantGen, ok: okSymbols, pending: [], rejected: [] },
    now,
    wantGen,
  );
  // A symbol is not Futu-live without a cached row (D10) — a live symbol
  // in this suite always has one.
  for (const s of okSymbols) r.recordTick(s, { last: 1 });
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
  const want = r.desiredWant(clientSymbols);
  assert.deepEqual(want.symbols, ["AAPL"]);
  assert.deepEqual(want.tiers, { AAPL: "held" });
});

test("want tiers = highest over the symbol's subscribers (held > view)", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  const clientSymbols = new Map([
    [futuClientA, new Set(["AAPL", "MSFT", "QQQ"])],
  ]);
  // subscriberTiers: symbol -> Map<client, tier>. AAPL is all-view; MSFT has
  // one held; QQQ's held comes from an ib-preference subscriber — the merge
  // counts every subscriber, not only futu-first ones.
  const subscriberTiers = new Map([
    ["AAPL", new Map([[futuClientA, "view"], [apiKeyClientB, "view"]])],
    ["MSFT", new Map([[futuClientA, "view"], [apiKeyClientB, "held"]])],
    ["QQQ", new Map([[futuClientA, "view"], [browserClient, "held"]])],
  ]);
  const want = r.desiredWant(clientSymbols, subscriberTiers);
  assert.deepEqual(want.symbols, ["AAPL", "MSFT", "QQQ"]);
  assert.deepEqual(want.tiers, { AAPL: "view", MSFT: "held", QQQ: "held" });
});

test("want tiers: no declared tiers defaults every symbol to held", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  const clientSymbols = new Map([[futuClientA, new Set(["AAPL"])]]);
  const want = r.desiredWant(clientSymbols);
  assert.deepEqual(want.tiers, { AAPL: "held" });
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

test("futuLive requires a cached row — a quiet symbol keeps IB demand", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  r.noteSocket(true);
  r.noteHealth({ opend_connected: true, quota: { ceiling: 800 } });
  r.applyAssign({ gen: 1, ok: ["AAPL"], pending: [], rejected: [] }, 0, 1);
  const subs = new Set([futuClientA]);
  const t = HD + 1;
  // Hold-down elapsed but no Futu row ever arrived: the symbol must NOT go
  // live — an IB frame would otherwise freeze (Fable F1) and hydration
  // would have nothing to send.
  assert.equal(r.futuLive("AAPL", t), false);
  assert.equal(r.ibDemand("AAPL", subs, t), true);
  // The first cached row flips it live and releases the IB line.
  r.recordTick("AAPL", { last: 10 });
  assert.equal(r.futuLive("AAPL", t), true);
  assert.equal(r.ibDemand("AAPL", subs, t), false);
});

test("isOk reflects the ok set of the latest accepted assign", () => {
  const r = setup();
  liveFutu(r, ["AAPL"], 0);
  assert.equal(r.isOk("AAPL"), true);
  assert.equal(r.isOk("MSFT"), false);
  r.applyAssign({ gen: 1, ok: ["MSFT"], pending: [], rejected: [] }, 10, 1);
  assert.equal(r.isOk("AAPL"), false);
  assert.equal(r.isOk("MSFT"), true);
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
  liveFutu(r, [], 0, 7); // up, but nothing in ok
  r.applyAssign(
    {
      gen: 7,
      ok: [],
      pending: [],
      rejected: [{ symbol: "HYG", reason: "quota" }],
    },
    10,
    7,
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
  r.noteHealth({ opend_connected: false, quota: null });
  assert.equal(r.futuLive("AAPL", t + 10), false);
  assert.equal(r.ibDemand("AAPL", subs, t + 10), true);
  // socket loss path
  r.noteHealth({ opend_connected: true, quota: {} });
  r.applyAssign(
    { gen: 2, ok: ["AAPL"], pending: [], rejected: [] },
    t + 20,
    2,
  );
  r.recordTick("AAPL", { last: 1 }); // fresh row after the down (D10)
  assert.equal(r.futuLive("AAPL", t + 20 + HD + 1), true);
  r.noteSocket(false);
  assert.equal(r.futuLive("AAPL", t + 20 + HD + 1), false);
});

test("health deadline marks futu down (stalled service)", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, ["AAPL"], 0);
  r.noteDown("health_deadline");
  assert.equal(r.isUp(), false);
  assert.equal(r.futuLive("AAPL", 100 + HD + 1), false);
});

test("recovery: fresh health + new ok + full hold-down", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, ["AAPL"], 0);
  r.noteDown("socket_close");
  assert.equal(r.futuLive("AAPL", 2000), false);
  // Recovery at t=1000 on a fresh want gen: ok at 1000, live only at 1000+HD
  r.noteSocket(true);
  r.noteHealth({ opend_connected: true, quota: {} });
  r.applyAssign({ gen: 1, ok: ["AAPL"], pending: [], rejected: [] }, 1000, 1);
  r.recordTick("AAPL", { last: 1 }); // fresh row after the down (D10)
  assert.equal(r.futuLive("AAPL", 1000 + HD - 1), false);
  assert.equal(r.futuLive("AAPL", 1000 + HD), true);
});

test("same-gen assign applies partition changes: pending→ok→session", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  r.noteSocket(true);
  r.noteHealth({ opend_connected: true, quota: {} });
  const G = 3; // the current want gen — the service sends several assigns for it

  // First assign: still pending (subscription not yet admitted).
  const p1 = r.applyAssign(
    { gen: G, ok: [], pending: [{ symbol: "AAPL", reason: "session" }], rejected: [] },
    0,
    G,
  );
  assert.equal(p1.ignored, false);
  assert.equal(r.futuLive("AAPL", HD + 10), false);

  // Same-gen assign completing the admission — accepted, hold-down starts now.
  const p2 = r.applyAssign(
    { gen: G, ok: ["AAPL"], pending: [], rejected: [] },
    100,
    G,
  );
  assert.deepEqual(p2.entered, ["AAPL"]);
  r.recordTick("AAPL", { last: 1 }); // first row after admission (D10)
  assert.equal(r.futuLive("AAPL", 100 + HD - 1), false);
  assert.equal(r.futuLive("AAPL", 100 + HD), true);

  // Same-gen session pullback — symbol leaves ok → back to IB immediately.
  r.applyAssign(
    { gen: G, ok: [], pending: [{ symbol: "AAPL", reason: "session" }], rejected: [] },
    2000,
    G,
  );
  assert.equal(r.futuLive("AAPL", 2000 + HD), false);
});

test("same-gen no-change assign does NOT restart the hold-down", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  r.noteSocket(true);
  r.noteHealth({ opend_connected: true, quota: {} });
  const G = 5;
  r.applyAssign({ gen: G, ok: ["AAPL"], pending: [], rejected: [] }, 0, G);
  r.recordTick("AAPL", { last: 1 }); // row cached during the hold-down (D10)
  // Periodic quota-refresh assign, same ok — okSince must stay at t=0.
  r.applyAssign({ gen: G, ok: ["AAPL"], pending: [], rejected: [] }, 900, G);
  assert.equal(r.futuLive("AAPL", HD), true);
});

test("symbol leaving ok needs a fresh hold-down to return", () => {
  const r = setup();
  r.setPref(futuClientA, "futu-first");
  liveFutu(r, ["AAPL"], 0, 2);
  r.applyAssign({ gen: 2, ok: [], pending: [], rejected: [] }, HD + 1, 2);
  assert.equal(r.futuLive("AAPL", HD + 2), false);
  r.applyAssign({ gen: 2, ok: ["AAPL"], pending: [], rejected: [] }, HD + 5, 2);
  r.recordTick("AAPL", { last: 1 }); // leaving ok also drops the cached row
  assert.equal(r.futuLive("AAPL", HD + 5 + HD - 1), false);
  assert.equal(r.futuLive("AAPL", HD + 5 + HD), true);
});

test("assigns for a gen other than the current want gen are rejected", () => {
  const r = setup();
  r.noteSocket(true);
  r.noteHealth({ opend_connected: true, quota: {} });
  // Current want gen is 3 — a delayed assign for the previous want (gen 2)
  // and a fabricated future gen are both ignored.
  assert.equal(
    r.applyAssign({ gen: 2, ok: ["AAPL"], pending: [], rejected: [] }, 0, 3)
      .ignored,
    true,
  );
  assert.equal(
    r.applyAssign({ gen: 4, ok: ["AAPL"], pending: [], rejected: [] }, 1, 3)
      .ignored,
    true,
  );
  assert.equal(r.futuLive("AAPL", HD + 10), false);
  // The current-gen assign applies.
  assert.equal(
    r.applyAssign({ gen: 3, ok: ["AAPL"], pending: [], rejected: [] }, 2, 3)
      .ignored,
    false,
  );
  r.recordTick("AAPL", { last: 1 }); // a live symbol also needs a row (D10)
  assert.equal(r.futuLive("AAPL", HD + 2), true);
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

test("down clears futu tick cache", () => {
  const r = setup();
  liveFutu(r, ["AAPL"], 0);
  r.recordTick("AAPL", { last: 1 });
  r.noteDown("socket_close");
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
  r.noteHealth({ opend_connected: true, quota: { remain: 800 } });
  assert.deepEqual(r.status(), {
    connected: true,
    opend_connected: true,
    quota: { remain: 800 },
  });
  r.noteDown("health_deadline");
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
