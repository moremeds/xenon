import { spawn } from "node:child_process";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer, WebSocket } from "ws";

// Shared e2e harness for the futu-routing tests: startStack boots the real
// relay subprocess under a caller-provided env against a fake futu-quotes
// service and a fake ticket validator. IB stays unreachable (--ib-port 1) so
// only the routing side is exercised.

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SERVER = path.join(HERE, "..", "ib_realtime_server.js");
export const API_KEY = "env-api-key";
export const HD = 400; // FUTU_HOLD_DOWN_MS
export const DEADLINE = 700; // FUTU_HEALTH_DEADLINE_MS
export const OPTION = {
  symbol: "AAPL",
  expiry: "20261218",
  strike: 500,
  right: "C",
};
export const OPTION_KEY = "AAPL_20261218_500_C";

const TIMING_ENV = {
  FUTU_PING_INTERVAL_MS: "150",
  FUTU_PONG_TIMEOUT_MS: "600",
  FUTU_HEALTH_DEADLINE_MS: String(DEADLINE),
  FUTU_RECONNECT_MS: "150",
  FUTU_HOLD_DOWN_MS: String(HD),
  FUTU_RECONCILE_MS: "80",
};

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function listen(server) {
  return new Promise((r) => server.listen(0, "127.0.0.1", r));
}

let stackN = 0;

export async function startStack(extraEnv = {}) {
  const runtimeFile = path.join(HERE, `e2e-runtime-stack-${++stackN}.json`);
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
    ws.statuses = () => msgs.filter((m) => m.type === "status");
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
