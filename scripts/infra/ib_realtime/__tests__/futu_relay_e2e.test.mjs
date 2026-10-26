import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer, WebSocket } from "ws";

// End-to-end: the real relay subprocess against a fake futu-quotes service
// (ws WebSocketServer) and a fake ticket validator. IB is deliberately
// unreachable (--ib-port 1) so every IB line stays down; what the tests
// verify is the Futu routing side: opt-in gating, want/assign handling,
// per-source delivery, failover within the health deadline, hold-down
// recovery, indexes pinned to IB, and providers.futu in status.
//
// The fake service keeps a periodic `health` stream like the real service —
// `futuHealthOn` toggles it to simulate a stall; scripted frames still work.

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SERVER = path.join(HERE, "..", "ib_realtime_server.js");
const RUNTIME_FILE = path.join(HERE, "e2e-runtime.json");
const API_KEY = "e2e-api-key";
const HD = 400; // FUTU_HOLD_DOWN_MS
const DEADLINE = 700; // FUTU_HEALTH_DEADLINE_MS

// Small knobs so the suite runs in seconds, not minutes.
const ENV = {
  FUTU_PING_INTERVAL_MS: "150",
  FUTU_PONG_TIMEOUT_MS: "600",
  FUTU_HEALTH_DEADLINE_MS: String(DEADLINE),
  FUTU_RECONNECT_MS: "150",
  FUTU_HOLD_DOWN_MS: String(HD),
  FUTU_RECONCILE_MS: "80",
};

let validator;
let futuWss;
let relay;
let relayPort;
let futuSock; // the service-side socket the relay connected with
const futuWants = [];
let relayStdout = "";
let healthTimer = null;
let futuHealthOn = false;
let futuHealthPayload = {
  type: "health",
  opend_connected: true,
  quota: { ceiling: 800, own_used: 0, all_used: 0, remain: 799 },
};

function json(res, code, body) {
  res.writeHead(code, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

function futuSend(msg) {
  if (futuSock?.readyState === WebSocket.OPEN) {
    futuSock.send(JSON.stringify(msg));
  }
}

function nextFutuSocket(timeoutMs = 4000) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(
      () => reject(new Error("no futu-quotes connection within timeout")),
      timeoutMs,
    );
    const check = () => {
      if (futuSock && futuSock.readyState === WebSocket.OPEN) {
        clearTimeout(t);
        resolve(futuSock);
      } else {
        setTimeout(check, 20);
      }
    };
    check();
  });
}

function waitFor(fn, { timeout = 4000, every = 25 } = {}) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeout;
    const poll = () => {
      try {
        const v = fn();
        if (v) {
          resolve(v);
          return;
        }
      } catch {
        /* keep polling */
      }
      if (Date.now() > deadline) {
        reject(new Error(`waitFor timed out\nrelay tail: ${relayStdout.slice(-1500)}`));
      } else {
        setTimeout(poll, every);
      }
    };
    poll();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Collect messages from a relay client socket.
function connectRelay({ apiKey, ticket, quotes, origin } = {}) {
  const params = [];
  if (ticket) params.push(`ticket=${ticket}`);
  if (quotes) params.push(`quotes=${quotes}`);
  const qs = params.length ? `?${params.join("&")}` : "";
  const headers = {};
  if (apiKey) headers["x-api-key"] = apiKey;
  if (origin !== undefined) headers.origin = origin;
  const ws = new WebSocket(`ws://127.0.0.1:${relayPort}/${qs}`, { headers });
  const msgs = [];
  ws.on("message", (raw) => {
    try {
      msgs.push(JSON.parse(raw.toString()));
    } catch {
      /* ignore */
    }
  });
  ws.msgs = msgs;
  ws.batches = () => msgs.filter((m) => m.type === "batch");
  ws.statuses = () => msgs.filter((m) => m.type === "status");
  return new Promise((resolve, reject) => {
    ws.on("open", () => resolve(ws));
    ws.on("error", reject);
    ws.on("unexpected-response", (_req, res) =>
      reject(new Error(`upgrade rejected: HTTP ${res.statusCode}`)),
    );
  });
}

function subscribe(ws, symbols, extra = {}) {
  ws.send(JSON.stringify({ action: "subscribe", symbols, ...extra }));
}

function lastFutuStatus(ws) {
  return ws.statuses().at(-1)?.providers?.futu ?? null;
}

// The first Futu row for a symbol flips the route and reaches the client via
// the hydration `price` frame (D10); rows after the flip arrive in `batch`.
function hasFutuTick(ws, symbol, last) {
  return ws.msgs.some(
    (m) =>
      m.updates?.[symbol]?.last === last ||
      (m.type === "price" && m.symbol === symbol && m.data?.last === last),
  );
}

async function admitOnFutu(symbols) {
  await waitFor(() =>
    symbols.every((s) => futuWants.at(-1)?.symbols?.includes(s)),
  );
  futuSend({
    type: "assign",
    gen: futuWants.at(-1).gen,
    ok: symbols,
    pending: [],
    rejected: [],
  });
}

before(async () => {
  validator = http.createServer((req, res) => {
    if (req.method === "POST" && req.url === "/ws-ticket/validate") {
      req.on("data", () => {});
      req.on("end", () => json(res, 200, {}));
      return;
    }
    json(res, 404, {});
  });
  await new Promise((r) => validator.listen(0, "127.0.0.1", r));
  const validatorPort = validator.address().port;

  futuWss = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  await new Promise((r) => futuWss.on("listening", r));
  const futuPort = futuWss.address().port;
  futuWss.on("connection", (sock) => {
    futuSock = sock;
    sock.on("message", (raw) => {
      let msg;
      try {
        msg = JSON.parse(raw.toString());
      } catch {
        return;
      }
      if (msg.action === "want") futuWants.push(msg);
    });
  });
  healthTimer = setInterval(() => {
    if (futuHealthOn) futuSend(futuHealthPayload);
  }, 200);

  const portSock = http.createServer();
  await new Promise((r) => portSock.listen(0, "127.0.0.1", r));
  relayPort = portSock.address().port;
  portSock.close();

  relay = spawn(
    process.execPath,
    [SERVER, "--port", String(relayPort), "--ib-port", "1"],
    {
      env: {
        ...process.env,
        TICKET_VALIDATE_URL: `http://127.0.0.1:${validatorPort}/ws-ticket/validate`,
        XENON_QUERY_API_KEY: API_KEY,
        IB_REALTIME_STATUS_TOKEN: "status-tok",
        FUTU_QUOTES_URL: `ws://127.0.0.1:${futuPort}/`,
        IB_REALTIME_RUNTIME_FILE: RUNTIME_FILE,
        ...ENV,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  relay.stdout.on("data", (d) => (relayStdout += d));
  relay.stderr.on("data", (d) => (relayStdout += d));
  await waitFor(() => relayStdout.includes("listening"), { timeout: 8000 });
  await nextFutuSocket();
});

after(() => {
  clearInterval(healthTimer);
  relay?.kill("SIGINT");
  validator?.close();
  futuWss?.close();
  try {
    fs.unlinkSync(RUNTIME_FILE);
  } catch {
    /* ignore */
  }
});

// ─── Tests ───────────────────────────────────────────────────────────────

test("opted-in API-key client receives futu ticks; un-opted do not", async () => {
  const opted = await connectRelay({ apiKey: API_KEY, quotes: "futu-first" });
  const unopted = await connectRelay({ apiKey: API_KEY });
  const browser = await connectRelay({
    ticket: "t1",
    quotes: "futu-first",
    origin: "http://localhost:3200",
  });
  subscribe(opted, ["AAPL"]);
  subscribe(unopted, ["AAPL"]);
  subscribe(browser, ["AAPL"]);

  await admitOnFutu(["AAPL"]);
  assert.equal(futuWants.at(-1).action, "want");
  assert.deepEqual(futuWants.at(-1).symbols, ["AAPL"]);
  assert.ok(futuWants.at(-1).gen >= 1);
  futuHealthOn = true; // real services keep the health stream alive

  // During the hold-down a futu tick is still not routed to anyone.
  futuSend({ type: "batch", updates: { AAPL: { symbol: "AAPL", last: 11.0 } } });
  await sleep(HD + 300);
  assert.equal(
    opted.batches().some((b) => b.updates?.AAPL?.last === 11.0),
    false,
    "futu tick delivered before hold-down elapsed",
  );

  futuSend({ type: "batch", updates: { AAPL: { symbol: "AAPL", last: 11.5 } } });
  await waitFor(
    () => opted.batches().some((b) => b.updates?.AAPL?.last === 11.5),
    { timeout: 3000 },
  );
  // Un-opted API-key client and browser client never see the futu tick.
  assert.equal(
    unopted.batches().some((b) => b.updates?.AAPL?.last === 11.5),
    false,
  );
  assert.equal(
    browser.batches().some((b) => b.updates?.AAPL?.last === 11.5),
    false,
  );

  // providers.futu shows the healthy link.
  await waitFor(() => lastFutuStatus(opted)?.connected === true, {
    timeout: 3000,
  });
  assert.equal(lastFutuStatus(opted).opend_connected, true);
  assert.ok(lastFutuStatus(opted).quota != null);
  opted.close();
  unopted.close();
  browser.close();
});

test("quota rejection keeps the symbol off Futu (IB demand stays)", async () => {
  futuHealthOn = true;
  const opted = await connectRelay({ apiKey: API_KEY, quotes: "futu-first" });
  subscribe(opted, ["HYG"]);
  await waitFor(() => futuWants.at(-1)?.symbols?.includes("HYG"));
  futuSend({
    type: "assign",
    gen: futuWants.at(-1).gen,
    ok: [],
    pending: [],
    rejected: [{ symbol: "HYG", reason: "quota" }],
  });
  await sleep(HD + 300);
  futuSend({ type: "batch", updates: { HYG: { symbol: "HYG", last: 80.1 } } });
  await sleep(300);
  assert.equal(
    opted.batches().some((b) => b.updates?.HYG),
    false,
    "rejected symbol must never receive futu ticks",
  );
  opted.close();
});

test("OpenD loss fails back to IB within the deadline", async () => {
  futuHealthOn = true;
  const opted = await connectRelay({ apiKey: API_KEY, quotes: "futu-first" });
  subscribe(opted, ["MSFT"]);
  await admitOnFutu(["MSFT"]);
  await sleep(HD + 300);
  futuSend({ type: "batch", updates: { MSFT: { symbol: "MSFT", last: 401 } } });
  await waitFor(() => hasFutuTick(opted, "MSFT", 401));

  // OpenD drops: the service reports opend_connected:false, then goes quiet.
  futuHealthOn = false;
  futuSend({ type: "health", opend_connected: false, quota: null });
  await waitFor(() => lastFutuStatus(opted)?.opend_connected === false, {
    timeout: 3000,
  });
  const n = opted.batches().length;
  futuSend({ type: "batch", updates: { MSFT: { symbol: "MSFT", last: 402 } } });
  await sleep(300);
  assert.equal(opted.batches().length, n);
  opted.close();
});

test("stalled service (no health) fails back within the health deadline", async () => {
  futuHealthOn = true;
  const opted = await connectRelay({ apiKey: API_KEY, quotes: "futu-first" });
  subscribe(opted, ["SPY"]);
  await admitOnFutu(["SPY"]);
  await sleep(HD + 300);
  futuSend({ type: "batch", updates: { SPY: { symbol: "SPY", last: 500 } } });
  await waitFor(() => hasFutuTick(opted, "SPY", 500));

  // Stall: the health stream stops entirely.
  futuHealthOn = false;
  const t0 = Date.now();
  await waitFor(() => lastFutuStatus(opted)?.opend_connected === false, {
    timeout: DEADLINE + 1500,
  });
  assert.ok(
    Date.now() - t0 < DEADLINE + 1500,
    "failover exceeded the health deadline",
  );
  const n = opted.batches().length;
  futuSend({ type: "batch", updates: { SPY: { symbol: "SPY", last: 501 } } });
  await sleep(300);
  assert.equal(opted.batches().length, n);
  opted.close();
});

test("recovery after hold-down re-routes to Futu", async () => {
  // The link is stalled from the previous test; make the down state explicit
  // and confirm it via status before recovering.
  futuHealthOn = false;
  const opted = await connectRelay({ apiKey: API_KEY, quotes: "futu-first" });
  subscribe(opted, ["QQQ"]);
  await waitFor(() => futuWants.at(-1)?.symbols?.includes("QQQ"));

  // The service comes back: health resumes, the client re-arms its want, a
  // fresh assign admits QQQ, and a full hold-down must elapse before ticks
  // flow again.
  const wantCount = futuWants.length;
  futuHealthOn = true;
  await waitFor(() => futuWants.length > wantCount, {
    timeout: DEADLINE + 2000,
  });
  futuSend({
    type: "assign",
    gen: futuWants.at(-1).gen,
    ok: ["QQQ"],
    pending: [],
    rejected: [],
  });
  await sleep(HD + 300);
  futuSend({ type: "batch", updates: { QQQ: { symbol: "QQQ", last: 480 } } });
  await waitFor(() => hasFutuTick(opted, "QQQ", 480), { timeout: 3000 });
  opted.close();
});

test("indexes never enter want and never receive futu ticks", async () => {
  futuHealthOn = true;
  const opted = await connectRelay({ apiKey: API_KEY, quotes: "futu-first" });
  subscribe(opted, [], {
    indexes: [{ symbol: "VIX", exchange: "CBOE" }],
  });
  await sleep(300);
  for (const w of futuWants) {
    assert.ok(!w.symbols.includes("VIX"), "VIX leaked into want");
  }
  // Even if the service volunteers it in ok (valid current-gen assign), the
  // pinned key never routes.
  futuSend({
    type: "assign",
    gen: futuWants.at(-1)?.gen ?? 0,
    ok: ["VIX"],
    pending: [],
    rejected: [],
  });
  await sleep(HD + 300);
  futuSend({ type: "batch", updates: { VIX: { symbol: "VIX", last: 18 } } });
  await sleep(300);
  assert.equal(
    opted.batches().some((b) => b.updates?.VIX),
    false,
    "index received a futu tick",
  );
  opted.close();
});

test("same-gen assign partition moves apply without a new want", async () => {
  futuHealthOn = true;
  const opted = await connectRelay({ apiKey: API_KEY, quotes: "futu-first" });
  subscribe(opted, ["COST"]);
  await waitFor(() => futuWants.at(-1)?.symbols?.includes("COST"));
  const gen = futuWants.at(-1).gen;

  // The service keeps the subscription pending, then completes it — SAME gen.
  futuSend({
    type: "assign",
    gen,
    ok: [],
    pending: [{ symbol: "COST", reason: "session" }],
    rejected: [],
  });
  await sleep(HD + 300);
  futuSend({ type: "batch", updates: { COST: { symbol: "COST", last: 900 } } });
  await sleep(200);
  assert.equal(
    opted.batches().some((b) => b.updates?.COST),
    false,
    "pending symbol delivered a futu tick",
  );

  futuSend({ type: "assign", gen, ok: ["COST"], pending: [], rejected: [] });
  await sleep(HD + 300);
  futuSend({ type: "batch", updates: { COST: { symbol: "COST", last: 901 } } });
  await waitFor(() => hasFutuTick(opted, "COST", 901));

  // Same-gen session pullback — ok→pending fails back to IB immediately.
  const n = opted.batches().length;
  futuSend({
    type: "assign",
    gen,
    ok: [],
    pending: [{ symbol: "COST", reason: "session" }],
    rejected: [],
  });
  await sleep(HD + 300);
  futuSend({ type: "batch", updates: { COST: { symbol: "COST", last: 902 } } });
  await sleep(300);
  assert.equal(
    opted.batches().length,
    n,
    "futu tick delivered after same-gen session pullback",
  );
  opted.close();
});

test("browser client cannot opt in even when the service claims ok", async () => {
  futuHealthOn = true;
  const browser = await connectRelay({
    ticket: "t2",
    quotes: "futu-first",
    origin: "http://localhost:3200",
  });
  subscribe(browser, ["WMT"]);
  await sleep(200);
  for (const w of futuWants) {
    assert.ok(
      !w.symbols.includes("WMT"),
      "browser-only symbol leaked into want",
    );
  }
  const n = browser.batches().length;
  futuSend({
    type: "assign",
    gen: futuWants.at(-1)?.gen ?? 0,
    ok: ["WMT"],
    pending: [],
    rejected: [],
  });
  await sleep(HD + 300);
  futuSend({ type: "batch", updates: { WMT: { symbol: "WMT", last: 90 } } });
  await sleep(300);
  assert.equal(browser.batches().length, n);
  browser.close();
});
