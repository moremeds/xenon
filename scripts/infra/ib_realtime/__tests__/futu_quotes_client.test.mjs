import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createFutuQuotesClient } from "../futu_quotes_client.js";
import { createFutuRouting } from "../futu_routing.js";

// Transport client for the relay -> futu-quotes internal WS. A FakeWs stands
// in for the service; fake timers drive ping/pong/health deadlines.

const PING = 200;
const PONG_TO = 500;
const HEALTH_DEADLINE = 800;
const RECONNECT = 300;

class FakeWs extends EventEmitter {
  constructor(url) {
    super();
    this.url = url;
    this.sent = [];
    this.pings = 0;
    this.closed = false;
    this.terminated = false;
    FakeWs.instances.push(this);
  }
  open() {
    queueMicrotask(() => this.emit("open"));
  }
  send(payload) {
    this.sent.push(JSON.parse(payload));
  }
  ping() {
    this.pings += 1;
  }
  close() {
    this.closed = true;
    this.emit("close");
  }
  terminate() {
    this.terminated = true;
    this.emit("close");
  }
  // test helpers — service side
  servicePong() {
    this.emit("pong");
  }
  serviceMessage(msg) {
    this.emit("message", Buffer.from(JSON.stringify(msg)));
  }
}

function setup(t, overrides = {}) {
  FakeWs.instances = [];
  const events = { assign: [], batch: [], health: [], down: [], socket: [] };
  const client = createFutuQuotesClient({
    url: "ws://127.0.0.1:9999/futu",
    WebSocketImpl: FakeWs,
    pingIntervalMs: PING,
    pongTimeoutMs: PONG_TO,
    healthDeadlineMs: HEALTH_DEADLINE,
    reconnectMs: RECONNECT,
    onAssign: (a) => events.assign.push(a),
    onBatch: (u) => events.batch.push(u),
    onHealth: (h) => events.health.push(h),
    onDown: (r) => events.down.push(r),
    onSocket: (open) => events.socket.push(open),
    ...overrides,
  });
  const ws = () => FakeWs.instances.at(-1);
  t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"], now: 0 });
  client.start();
  ws().open();
  return { client, events, ws };
}

const tick = async (t, ms) => {
  t.mock.timers.tick(ms);
  await Promise.resolve();
};

test("want sent on open with gen=1; setWant sends only on change", async (t) => {
  const { client, ws } = setup(t);
  await tick(t, 1);
  client.setWant(["AAPL", "HYG"]);
  assert.deepEqual(ws().sent, [
    { action: "want", gen: 1, symbols: ["AAPL", "HYG"] },
  ]);
  client.setWant(["AAPL", "HYG"]); // unchanged — no resend
  client.setWant(["AAPL", "HYG", "MSFT"]); // changed — gen++
  assert.equal(ws().sent.length, 2);
  assert.deepEqual(ws().sent[1], {
    action: "want",
    gen: 2,
    symbols: ["AAPL", "HYG", "MSFT"],
  });
});

test("assign/health/batch frames dispatch to callbacks", async (t) => {
  const { client, events, ws } = setup(t);
  await tick(t, 1);
  client.setWant(["AAPL"]); // want gen 1 is now the current gen
  ws().serviceMessage({ type: "assign", gen: 1, ok: ["AAPL"], pending: [], rejected: [] });
  ws().serviceMessage({ type: "health", opend_connected: true, quota: { remain: 9 } });
  ws().serviceMessage({ type: "batch", updates: { AAPL: { last: 1 } } });
  assert.equal(events.assign.length, 1);
  assert.equal(events.health.length, 1);
  assert.deepEqual(events.batch, [{ AAPL: { last: 1 } }]);
});

test("protocol pings go out every interval; pongs keep the link alive", async (t) => {
  const { ws } = setup(t);
  await tick(t, 1);
  for (let i = 0; i < 5; i++) {
    await tick(t, PING);
    ws().servicePong();
  }
  assert.equal(ws().pings, 5);
  assert.equal(ws().terminated, false);
});

test("pong timeout tears the socket down and reconnects", async (t) => {
  const { client, events, ws } = setup(t);
  await tick(t, 1);
  await tick(t, PONG_TO + PING + 1); // several pings, never a pong
  assert.equal(ws().terminated, true);
  assert.ok(events.down.includes("pong_timeout"));
  await tick(t, RECONNECT);
  assert.equal(FakeWs.instances.length, 2); // new socket, new epoch
  ws().open();
  await tick(t, 1);
  client.setWant(["AAPL"]);
  assert.equal(ws().sent[0].gen, 1); // gen restarts per connection
});

test("health deadline with socket still open marks futu down", async (t) => {
  const { client, events, ws } = setup(t);
  await tick(t, 1);
  client.setWant(["AAPL"]);
  ws().serviceMessage({ type: "health", opend_connected: true, quota: {} });
  // service stalls: keep answering protocol pongs, send no health
  for (let i = 0; i < 6; i++) {
    await tick(t, PING);
    ws().servicePong();
  }
  assert.ok(events.down.includes("health_deadline"));
  assert.equal(ws().closed, false); // socket itself stays open
});

test("recovery: fresh health re-arms the want set", async (t) => {
  const { client, events, ws } = setup(t);
  await tick(t, 1);
  client.setWant(["AAPL"]);
  ws().serviceMessage({ type: "health", opend_connected: true, quota: {} });
  for (let i = 0; i < 6; i++) {
    await tick(t, PING);
    ws().servicePong();
  }
  assert.ok(events.down.includes("health_deadline"));
  const sentBefore = ws().sent.length;
  ws().serviceMessage({ type: "health", opend_connected: true, quota: {} });
  assert.equal(ws().sent.length, sentBefore + 1); // want re-sent
  assert.equal(ws().sent.at(-1).action, "want");
});

test("opend_connected:false health is an immediate down", async (t) => {
  const { events, ws } = setup(t);
  await tick(t, 1);
  ws().serviceMessage({ type: "health", opend_connected: true, quota: {} });
  ws().serviceMessage({ type: "health", opend_connected: false, quota: null });
  assert.ok(events.down.includes("opend_lost"));
});

test("R234-1: assigns for a non-current want gen are not dispatched", async (t) => {
  const { client, events, ws } = setup(t);
  await tick(t, 1);
  client.setWant(["AAPL"]); // want gen 1 is now current
  // Stale (previous epoch) and fabricated (future) gens are dropped.
  ws().serviceMessage({ type: "assign", gen: 0, ok: ["AAPL"], pending: [], rejected: [] });
  ws().serviceMessage({ type: "assign", gen: 99, ok: ["AAPL"], pending: [], rejected: [] });
  assert.equal(events.assign.length, 0);
  // The current want gen dispatches — and repeats of it do too.
  ws().serviceMessage({ type: "assign", gen: 1, ok: [], pending: [{ symbol: "AAPL", reason: "session" }], rejected: [] });
  ws().serviceMessage({ type: "assign", gen: 1, ok: ["AAPL"], pending: [], rejected: [] });
  assert.equal(events.assign.length, 2);
  assert.equal(events.assign[1].gen, 1);
  // A new want moves the fence forward: gen 1 is now stale.
  client.setWant(["AAPL", "MSFT"]); // gen 2
  ws().serviceMessage({ type: "assign", gen: 1, ok: ["AAPL"], pending: [], rejected: [] });
  assert.equal(events.assign.length, 2);
  ws().serviceMessage({ type: "assign", gen: 2, ok: ["AAPL", "MSFT"], pending: [], rejected: [] });
  assert.equal(events.assign.length, 3);
});

test("R234-2: default constants — down exactly at the 15s health deadline while pongs continue", async (t) => {
  // No overrides → production defaults (ping 10 s, pong 20 s, deadline 15 s).
  FakeWs.instances = [];
  const events = { down: [] };
  let downAt = null;
  const client = createFutuQuotesClient({
    url: "ws://127.0.0.1:9999/futu",
    WebSocketImpl: FakeWs,
    onDown: (r) => {
      events.down.push(r);
      downAt = Date.now();
    },
  });
  t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"], now: 0 });
  client.start();
  const ws = FakeWs.instances.at(-1);
  ws.open();
  await tick(t, 1); // t = 1, socket open
  ws.serviceMessage({ type: "health", opend_connected: true, quota: {} }); // armed at t=1
  // Protocol pongs keep arriving; only the health stream stalls.
  let firedAt = null;
  for (let i = 0; i < 16; i++) {
    await tick(t, 1000);
    if (events.down.length && firedAt == null) firedAt = Date.now();
    ws.servicePong();
  }
  assert.ok(events.down.includes("health_deadline"));
  assert.equal(downAt, 15_001); // armed at t=1 → deadline at exactly +15 s
  assert.equal(ws.terminated, false); // live pongs keep the socket up
  client.stop();
});

test("R234-2: recovery still needs the full 60s hold-down (default values)", async (t) => {
  // Wire the client to the real routing module exactly like the server does.
  FakeWs.instances = [];
  const routing = createFutuRouting(); // default FUTU_HOLD_DOWN_MS = 60s
  let client;
  client = createFutuQuotesClient({
    url: "ws://127.0.0.1:9999/futu",
    WebSocketImpl: FakeWs,
    onAssign: (a) => routing.applyAssign(a, Date.now(), client.sentGen()),
    onHealth: (h) => routing.noteHealth(h),
    onDown: (r) => routing.noteDown(r),
    onSocket: (o) => routing.noteSocket(o),
  });
  const c = { name: "svc" };
  routing.setPref(c, "futu-first");
  t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"], now: 0 });
  client.start();
  const ws = FakeWs.instances.at(-1);
  ws.open();
  await tick(t, 1);
  // First health of the epoch: want is empty, so nothing is sent.
  ws.serviceMessage({ type: "health", opend_connected: true, quota: {} });
  client.setWant(["AAPL"]); // want gen 1
  ws.serviceMessage({ type: "assign", gen: 1, ok: ["AAPL"], pending: [], rejected: [] });
  // Stall the health stream while protocol pongs continue → down at 15 s.
  for (let i = 0; i < 16; i++) {
    await tick(t, 1000);
    ws.servicePong();
  }
  assert.equal(routing.isUp(), false);
  // Recovery: fresh health re-arms want (gen 2); new ok needs its own 60 s.
  const tRecover = Date.now();
  ws.serviceMessage({ type: "health", opend_connected: true, quota: {} });
  assert.equal(ws.sent.at(-1).action, "want");
  assert.equal(ws.sent.at(-1).gen, 2);
  ws.serviceMessage({ type: "assign", gen: 2, ok: ["AAPL"], pending: [], rejected: [] });
  // A real service streams health continuously — it must keep streaming
  // through the hold-down or the 15 s deadline fires again. Protocol pongs
  // keep the socket up (20 s pong timeout).
  for (let i = 0; i < 11; i++) {
    ws.serviceMessage({ type: "health", opend_connected: true, quota: {} });
    ws.servicePong();
    await tick(t, 5000);
  }
  assert.equal(routing.futuLive("AAPL", Date.now()), false); // 55 s in
  ws.serviceMessage({ type: "health", opend_connected: true, quota: {} });
  ws.servicePong();
  await tick(t, 5000);
  assert.equal(routing.futuLive("AAPL", Date.now()), true); // 60 s elapsed
  assert.ok(Date.now() - tRecover >= 60_000);
  client.stop();
});
