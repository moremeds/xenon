import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer, WebSocket } from "ws";

// Relay env flags, read once at startup:
//   XENON_QUOTES_DEFAULT=ib|futu-first (default ib) — the quote preference for
//     EVERY client, browser ticket connections included. The api-key
//     ?quotes=futu-first opt-in is unchanged; there is no ?quotes=ib.
//   XENON_FUTU_OPTIONS=0|1 (default 0) — 0 keeps option contract keys pinned
//     to IB; 1 routes them like stocks and lets them into `want`. Indexes,
//     forex and non-US native venues stay pinned either way.
// Each test boots the real relay subprocess under its own env against a fake
// futu-quotes service and a fake ticket validator; IB stays unreachable
// (--ib-port 1) so only the routing side is exercised.

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SERVER = path.join(HERE, "..", "ib_realtime_server.js");
const API_KEY = "env-api-key";
const HD = 400; // FUTU_HOLD_DOWN_MS
const DEADLINE = 700; // FUTU_HEALTH_DEADLINE_MS
const OPTION_KEY = "AAPL_20261218_500_C";
const OPTION = { symbol: "AAPL", expiry: "20261218", strike: 500, right: "C" };

const TIMING_ENV = {
  FUTU_PING_INTERVAL_MS: "150",
  FUTU_PONG_TIMEOUT_MS: "600",
  FUTU_HEALTH_DEADLINE_MS: String(DEADLINE),
  FUTU_RECONNECT_MS: "150",
  FUTU_HOLD_DOWN_MS: String(HD),
  FUTU_RECONCILE_MS: "80",
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function listen(server) {
  return new Promise((r) => server.listen(0, "127.0.0.1", r));
}

let stackN = 0;

async function startStack(extraEnv = {}) {
  const runtimeFile = path.join(HERE, `e2e-runtime-env-${++stackN}.json`);
  let relayStdout = "";

  const waitFor = (fn, { timeout = 4000, every = 25 } = {}) =>
    new Promise((resolve, reject) => {
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
          reject(
            new Error(
              `waitFor timed out\nrelay tail: ${relayStdout.slice(-1500)}`,
            ),
          );
        } else {
          setTimeout(poll, every);
        }
      };
      poll();
    });

  const validator = http.createServer((req, res) => {
    if (req.method === "POST" && req.url === "/ws-ticket/validate") {
      req.on("data", () => {});
      req.on("end", () => {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end("{}");
      });
      return;
    }
    res.writeHead(404).end();
  });
  await listen(validator);

  const futuWss = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  await new Promise((r) => futuWss.on("listening", r));
  const wants = [];
  let futuSock = null;
  futuWss.on("connection", (sock) => {
    futuSock = sock;
    sock.on("message", (raw) => {
      let msg;
      try {
        msg = JSON.parse(raw.toString());
      } catch {
        return;
      }
      if (msg.action === "want") wants.push(msg);
    });
  });
  // A healthy service keeps the health stream alive — arm it before the relay
  // connects so `up` goes true on the first frame after the socket opens.
  const healthTimer = setInterval(() => {
    if (futuSock?.readyState === WebSocket.OPEN) {
      futuSock.send(
        JSON.stringify({
          type: "health",
          opend_connected: true,
          quota: { ceiling: 800, own_used: 0, all_used: 0, remain: 799 },
        }),
      );
    }
  }, 200);
  const futuSend = (msg) => {
    if (futuSock?.readyState === WebSocket.OPEN) {
      futuSock.send(JSON.stringify(msg));
    }
  };

  const portSock = http.createServer();
  await listen(portSock);
  const relayPort = portSock.address().port;
  portSock.close();

  const relay = spawn(
    process.execPath,
    [SERVER, "--port", String(relayPort), "--ib-port", "1"],
    {
      env: {
        ...process.env,
        TICKET_VALIDATE_URL: `http://127.0.0.1:${validator.address().port}/ws-ticket/validate`,
        XENON_QUERY_API_KEY: API_KEY,
        IB_REALTIME_STATUS_TOKEN: "status-tok",
        FUTU_QUOTES_URL: `ws://127.0.0.1:${futuWss.address().port}/`,
        IB_REALTIME_RUNTIME_FILE: runtimeFile,
        ...TIMING_ENV,
        ...extraEnv,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  relay.stdout.on("data", (d) => (relayStdout += d));
  relay.stderr.on("data", (d) => (relayStdout += d));
  await waitFor(() => relayStdout.includes("listening"), { timeout: 8000 });
  await waitFor(() => futuSock?.readyState === WebSocket.OPEN);

  const connect = ({ apiKey, ticket, quotes, origin } = {}) => {
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
    return new Promise((resolve, reject) => {
      ws.on("open", () => resolve(ws));
      ws.on("error", reject);
      ws.on("unexpected-response", (_req, res) =>
        reject(new Error(`upgrade rejected: HTTP ${res.statusCode}`)),
      );
    });
  };

  const admitOnFutu = async (symbols) => {
    await waitFor(() =>
      symbols.every((s) => wants.at(-1)?.symbols?.includes(s)),
    );
    futuSend({
      type: "assign",
      gen: wants.at(-1).gen,
      ok: symbols,
      pending: [],
      rejected: [],
    });
  };

  const close = () => {
    clearInterval(healthTimer);
    relay?.kill("SIGINT");
    validator?.close();
    futuWss?.close();
    try {
      fs.unlinkSync(runtimeFile);
    } catch {
      /* ignore */
    }
  };

  return { wants, futuSend, connect, admitOnFutu, waitFor, close };
}

// ─── XENON_QUOTES_DEFAULT ─────────────────────────────────────────────────

test("XENON_QUOTES_DEFAULT=futu-first routes every client incl. tickets to Futu", async (t) => {
  const stack = await startStack({ XENON_QUOTES_DEFAULT: "futu-first" });
  t.after(stack.close);

  const browser = await stack.connect({
    ticket: "t1",
    origin: "http://localhost:3200",
  });
  const apiPlain = await stack.connect({ apiKey: API_KEY });
  // There is no ?quotes=ib opt-out — an api-key client asking for it still
  // gets the env default.
  const apiIb = await stack.connect({ apiKey: API_KEY, quotes: "ib" });
  const all = [browser, apiPlain, apiIb];
  for (const ws of all) {
    ws.send(JSON.stringify({ action: "subscribe", symbols: ["AAPL"] }));
  }

  await stack.admitOnFutu(["AAPL"]);
  await sleep(HD + 300);
  stack.futuSend({
    type: "batch",
    updates: { AAPL: { symbol: "AAPL", last: 11.5 } },
  });
  for (const ws of all) {
    await stack.waitFor(
      () => ws.batches().some((b) => b.updates?.AAPL?.last === 11.5),
      { timeout: 3000 },
    );
  }
  for (const ws of all) ws.close();
});

test("XENON_QUOTES_DEFAULT=ib keeps tickets on IB; api-key opt-in unchanged", async (t) => {
  const stack = await startStack({ XENON_QUOTES_DEFAULT: "ib" });
  t.after(stack.close);

  const browser = await stack.connect({
    ticket: "t2",
    quotes: "futu-first",
    origin: "http://localhost:3200",
  });
  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  browser.send(JSON.stringify({ action: "subscribe", symbols: ["WMT"] }));
  opted.send(JSON.stringify({ action: "subscribe", symbols: ["MSFT"] }));

  // The ticket client's symbol never enters want even though it asked for
  // futu-first; the api-key opt-in still does.
  await stack.admitOnFutu(["MSFT"]);
  await sleep(300);
  for (const w of stack.wants) {
    assert.ok(!w.symbols.includes("WMT"), "ticket symbol leaked into want");
  }

  await sleep(HD + 300);
  stack.futuSend({
    type: "batch",
    updates: {
      MSFT: { symbol: "MSFT", last: 401 },
      WMT: { symbol: "WMT", last: 90 },
    },
  });
  await stack.waitFor(
    () => opted.batches().some((b) => b.updates?.MSFT?.last === 401),
    { timeout: 3000 },
  );
  await sleep(200);
  assert.equal(
    browser.batches().some((b) => b.updates?.WMT || b.updates?.MSFT),
    false,
    "ticket client received a futu tick under default=ib",
  );
  browser.close();
  opted.close();
});

test("unrecognized XENON_QUOTES_DEFAULT falls back to ib", async (t) => {
  const stack = await startStack({ XENON_QUOTES_DEFAULT: "garbage" });
  t.after(stack.close);

  const browser = await stack.connect({
    ticket: "t3",
    quotes: "futu-first",
    origin: "http://localhost:3200",
  });
  browser.send(JSON.stringify({ action: "subscribe", symbols: ["XYZ"] }));
  await sleep(400);
  for (const w of stack.wants) {
    assert.ok(!w.symbols.includes("XYZ"), "ticket symbol leaked into want");
  }
  browser.close();
});

// ─── XENON_FUTU_OPTIONS ──────────────────────────────────────────────────

test("XENON_FUTU_OPTIONS=0 keeps option keys pinned to IB", async (t) => {
  const stack = await startStack({ XENON_FUTU_OPTIONS: "0" });
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  opted.send(
    JSON.stringify({
      action: "subscribe",
      symbols: ["SPY"],
      contracts: [OPTION],
    }),
  );

  // SPY enters want; the option key never does.
  await stack.admitOnFutu(["SPY"]);
  await sleep(300);
  for (const w of stack.wants) {
    assert.ok(
      !w.symbols.includes(OPTION_KEY),
      "option key leaked into want under XENON_FUTU_OPTIONS=0",
    );
  }

  // Even if the service volunteers the key in ok, the pinned key never
  // routes — same guard as the index test in futu_relay_e2e.
  stack.futuSend({
    type: "assign",
    gen: stack.wants.at(-1).gen,
    ok: ["SPY", OPTION_KEY],
    pending: [],
    rejected: [],
  });
  await sleep(HD + 300);
  stack.futuSend({
    type: "batch",
    updates: { [OPTION_KEY]: { symbol: OPTION_KEY, last: 5.5 } },
  });
  await sleep(300);
  assert.equal(
    opted.batches().some((b) => b.updates?.[OPTION_KEY]),
    false,
    "pinned option key received a futu tick",
  );
  opted.close();
});

test("XENON_FUTU_OPTIONS=1 sends option keys in want and routes their ticks", async (t) => {
  const stack = await startStack({ XENON_FUTU_OPTIONS: "1" });
  t.after(stack.close);

  const opted = await stack.connect({ apiKey: API_KEY, quotes: "futu-first" });
  const browser = await stack.connect({
    ticket: "t4",
    origin: "http://localhost:3200",
  });
  opted.send(
    JSON.stringify({
      action: "subscribe",
      contracts: [OPTION],
      indexes: [{ symbol: "VIX", exchange: "CBOE" }],
      forexes: [{ base: "USD", quote: "JPY" }],
      stocks: [{ symbol: "7203", exchange: "TSEJ", currency: "JPY" }],
    }),
  );
  // A ticket client holding the same option keeps IB demand and stays on IB.
  browser.send(JSON.stringify({ action: "subscribe", contracts: [OPTION] }));

  // The option key enters want like a stock symbol; every other pin holds.
  await stack.waitFor(() =>
    stack.wants.at(-1)?.symbols?.includes(OPTION_KEY),
  );
  for (const w of stack.wants) {
    assert.ok(!w.symbols.includes("VIX"), "index leaked into want");
    assert.ok(!w.symbols.includes("USD.JPY"), "forex leaked into want");
    assert.ok(!w.symbols.includes("7203"), "non-US stock leaked into want");
  }
  await stack.admitOnFutu([OPTION_KEY]);
  await sleep(HD + 300);
  stack.futuSend({
    type: "batch",
    updates: { [OPTION_KEY]: { symbol: OPTION_KEY, last: 5.5 } },
  });
  await stack.waitFor(
    () => opted.batches().some((b) => b.updates?.[OPTION_KEY]?.last === 5.5),
    { timeout: 3000 },
  );
  await sleep(200);
  assert.equal(
    browser.batches().some((b) => b.updates?.[OPTION_KEY]),
    false,
    "ib-preference client received the futu option tick",
  );
  opted.close();
  browser.close();
});
