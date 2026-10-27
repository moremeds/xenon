import { spawn } from "node:child_process";
import http from "node:http";
import fs from "node:fs";
import os from "node:os";
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

// Hermetic relay env: only PATH plus what the test passes. The parent env is
// never inherited, and IB_REALTIME_NO_DOTENV=1 stops the relay loading the
// repo .env, so exported shell vars or a configured checkout cannot change
// routing flags, the IB host, or tokens under test.
export function relayEnv(extra = {}) {
  return {
    PATH: process.env.PATH ?? "",
    IB_REALTIME_NO_DOTENV: "1",
    IB_GATEWAY_HOST: "127.0.0.1",
    ...extra,
  };
}

// Unique per-call runtime dir (fs.mkdtemp) so two stacks or test files never
// share a runtime file. `cleanup` removes the dir.
export function makeRuntimeFile() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "xenon-relay-test-"));
  return {
    file: path.join(dir, "runtime.json"),
    cleanup: () => fs.rmSync(dir, { recursive: true, force: true }),
  };
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function listen(server) {
  return new Promise((r) => server.listen(0, "127.0.0.1", r));
}

// Boots the stack, retrying startup when the relay child loses the
// reserve-then-release race on its ephemeral port (listen EADDRINUSE). Each
// attempt cleans up after itself; the rejection carries `runtimeDir`, the dir
// that attempt owned (already removed).
export async function startStack(extraEnv = {}) {
  const ATTEMPTS = 3;
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await bootStack(extraEnv);
    } catch (err) {
      if (!err?.portInUse || attempt >= ATTEMPTS) throw err;
    }
  }
}

async function bootStack(extraEnv) {
  const { file: runtimeFile, cleanup: cleanupRuntime } = makeRuntimeFile();
  const runtimeDir = path.dirname(runtimeFile);
  let relayStdout = "";
  let validator;
  let futuWss;
  let healthTimer;
  let relay;

  // Idempotent and safe on a half-built stack: also the failure path when
  // startup rejects, so no child, mock server, timer or tmp dir is leaked.
  const close = () => {
    clearInterval(healthTimer);
    relay?.kill("SIGINT");
    validator?.close();
    futuWss?.close();
    cleanupRuntime();
  };

  try {
    // `signal` cancels the poll: the pending timer is cleared and the promise
    // rejects, so a losing race leg leaves no timeout behind.
    const waitFor = (fn, { timeout = 4000, every = 25, signal } = {}) =>
      new Promise((resolve, reject) => {
        const deadline = Date.now() + timeout;
        let timer;
        signal?.addEventListener("abort", () => {
          clearTimeout(timer);
          reject(new Error("waitFor aborted"));
        });
        const poll = () => {
          if (signal?.aborted) return;
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
            timer = setTimeout(poll, every);
          }
        };
        poll();
      });

    validator = http.createServer((req, res) => {
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

    futuWss = new WebSocketServer({ host: "127.0.0.1", port: 0 });
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
    healthTimer = setInterval(() => {
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

    relay = spawn(
      process.execPath,
      [SERVER, "--port", String(relayPort), "--ib-port", "1"],
      {
        env: relayEnv({
          TICKET_VALIDATE_URL: `http://127.0.0.1:${validator.address().port}/ws-ticket/validate`,
          XENON_QUERY_API_KEY: API_KEY,
          IB_REALTIME_STATUS_TOKEN: "status-tok",
          FUTU_QUOTES_URL: `ws://127.0.0.1:${futuWss.address().port}/`,
          IB_REALTIME_RUNTIME_FILE: runtimeFile,
          ...TIMING_ENV,
          ...extraEnv,
        }),
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    relay.stdout.on("data", (d) => (relayStdout += d));
    relay.stderr.on("data", (d) => (relayStdout += d));
    // Reject at once if the relay exits before it listens (bad env, crash).
    const exited = new Promise((_, reject) =>
      relay.once("exit", (code, signal) => {
        const err = new Error(
          `relay exited before listening (code=${code} signal=${signal})\nrelay tail: ${relayStdout.slice(-1500)}`,
        );
        err.portInUse = relayStdout.includes("EADDRINUSE");
        reject(err);
      }),
    );
    exited.catch(() => {}); // no unhandled rejection after startup succeeds
    const readyAbort = new AbortController();
    try {
      await Promise.race([
        waitFor(() => relayStdout.includes("listening"), {
          timeout: 8000,
          signal: readyAbort.signal,
        }),
        exited,
      ]);
    } finally {
      readyAbort.abort(); // cancel the losing poll leg
    }
    await waitFor(() => futuSock?.readyState === WebSocket.OPEN);

    const connect = ({ apiKey, ticket, quotes, origin } = {}) => {
      const params = [];
      if (ticket) params.push(`ticket=${ticket}`);
      if (quotes) params.push(`quotes=${quotes}`);
      const qs = params.length ? `?${params.join("&")}` : "";
      const headers = {};
      if (apiKey) headers["x-api-key"] = apiKey;
      if (origin !== undefined) headers.origin = origin;
      const ws = new WebSocket(`ws://127.0.0.1:${relayPort}/${qs}`, {
        headers,
      });
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

    // A served Futu row reaches the client either as a hydration `price` frame
    // (route flip) or a `batch` update (steady state) — wait for the VALUE,
    // never the frame kind or the timing of the flip.
    const futuValue = (ws, symbol, last) =>
      ws.msgs.some(
        (m) =>
          m.updates?.[symbol]?.last === last ||
          (m.type === "price" && m.symbol === symbol && m.data?.last === last),
      );
    const waitFutuValue = (ws, symbol, last, opts) =>
      waitFor(() => futuValue(ws, symbol, last), opts);
    // Any frame (price or batch) mentioning the symbol — for negative asserts
    // that must not pass just because a leaked row rode the `price` channel.
    const hasFutuFrame = (ws, symbol) =>
      ws.msgs.some((m) => m.symbol === symbol || m.updates?.[symbol] != null);

    return {
      wants,
      futuSend,
      connect,
      admitOnFutu,
      waitFor,
      waitFutuValue,
      hasFutuFrame,
      close,
    };
  } catch (err) {
    close();
    err.runtimeDir = runtimeDir;
    throw err;
  }
}
