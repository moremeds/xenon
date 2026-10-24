import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import {
  createLiveness,
  PING_INTERVAL_MS,
  PONG_TIMEOUT_MS,
} from "../liveness.js";

class FakeWs extends EventEmitter {
  constructor() {
    super();
    this.protocolPings = 0;
    this.closed = false;
  }
  ping() {
    this.protocolPings += 1;
  }
  close() {
    this.closed = true;
  }
}

function setup() {
  const clients = new Set();
  const appPings = [];
  const aliveCalls = [];
  const liveness = createLiveness({
    clients,
    sendAppPing: (c) => appPings.push(c),
    onAlive: (c, nowMs) => aliveCalls.push(nowMs),
    closeClient: (c) => c.close(),
  });
  return { clients, appPings, aliveCalls, liveness };
}

// A client that answers only protocol pings (like argon's Python `websockets`,
// which also pings every 20 s) must NOT be closed after 90 s.
test("protocol-pong-only client is not closed after 90s", (t) => {
  t.mock.timers.enable({ apis: ["setInterval", "Date"], now: 0 });
  const { clients, appPings, liveness } = setup();
  const client = new FakeWs();
  clients.add(client);
  liveness.attach(client);
  liveness.start();

  for (let i = 0; i < 4; i++) {
    t.mock.timers.tick(PING_INTERVAL_MS); // sweep: app ping + ws.ping()
    client.emit("pong"); // ws-stack auto-answer to the protocol ping
  }
  // 120 s of protocol pongs — well past the 65 s dead-peer budget.
  assert.equal(client.closed, false);
  assert.ok(appPings.length >= 4);
  assert.ok(client.protocolPings >= 4);
});

// A client that only sends protocol pings (never pongs, never app traffic)
// is also alive.
test("protocol-ping-only client is not closed after 90s", (t) => {
  t.mock.timers.enable({ apis: ["setInterval", "Date"], now: 0 });
  const { clients, liveness } = setup();
  const client = new FakeWs();
  clients.add(client);
  liveness.attach(client);
  liveness.start();

  for (let i = 0; i < 4; i++) {
    client.emit("ping"); // client-initiated protocol ping (ws auto-replies pong)
    t.mock.timers.tick(PING_INTERVAL_MS);
  }
  assert.equal(client.closed, false);
});

// A truly silent client IS closed once it exceeds the pong timeout.
test("silent client is closed after the pong timeout", (t) => {
  t.mock.timers.enable({ apis: ["setInterval", "Date"], now: 0 });
  const { clients, liveness } = setup();
  const client = new FakeWs();
  clients.add(client);
  liveness.attach(client);
  liveness.start();

  t.mock.timers.tick(PONG_TIMEOUT_MS + PING_INTERVAL_MS);
  assert.equal(client.closed, true);
  assert.equal(liveness.lastAlive.has(client), false);
});

// App-level {action:"pong"} keeps refreshing liveness (browser path unchanged).
test("app pong via noteAlive refreshes liveness", (t) => {
  t.mock.timers.enable({ apis: ["setInterval", "Date"], now: 0 });
  const { clients, aliveCalls, liveness } = setup();
  const client = new FakeWs();
  clients.add(client);
  liveness.attach(client);
  liveness.start();

  for (let i = 0; i < 4; i++) {
    t.mock.timers.tick(PING_INTERVAL_MS);
    liveness.noteAlive(client);
  }
  assert.equal(client.closed, false);
  assert.ok(aliveCalls.length >= 4);
});

test("detach drops liveness tracking", () => {
  const { clients, liveness } = setup();
  const client = new FakeWs();
  clients.add(client);
  liveness.attach(client);
  assert.equal(liveness.lastAlive.has(client), true);
  liveness.detach(client);
  assert.equal(liveness.lastAlive.has(client), false);
});
