// Relay -> futu-quotes internal WS client (docs/architecture/futu-quotes.md).
//
// Sends `want` (complete desired set, no deltas; `gen` is per-connection and
// starts at 1) and receives `assign`/`batch`/`health`. Liveness is protocol
// ping/pong: we ping every pingIntervalMs and tear down the socket when no
// pong arrives within pongTimeoutMs. Independently, when no `health` frame
// arrives within healthDeadlineMs the link is reported down (a stalled
// service) — the socket stays open and may recover on a fresh health frame.
// `WebSocketImpl` is injectable so tests can drive a fake service with fake
// timers; there are no Date/timer reads outside the tick loop and events.

import { WebSocket } from "ws";

export const FUTU_PING_INTERVAL_MS = 10_000;
export const FUTU_PONG_TIMEOUT_MS = 20_000;
export const FUTU_HEALTH_DEADLINE_MS = 15_000;
export const FUTU_RECONNECT_MS = 5_000;

export function createFutuQuotesClient({
  url,
  WebSocketImpl = WebSocket,
  pingIntervalMs = FUTU_PING_INTERVAL_MS,
  pongTimeoutMs = FUTU_PONG_TIMEOUT_MS,
  healthDeadlineMs = FUTU_HEALTH_DEADLINE_MS,
  reconnectMs = FUTU_RECONNECT_MS,
  onAssign = () => {},
  onBatch = () => {},
  onHealth = () => {},
  onDown = () => {},
  onSocket = () => {},
  log = () => {},
}) {
  let ws = null;
  let open = false;
  let healthy = false; // a fresh health frame arrived this epoch
  let lastPongAt = 0;
  let gen = 0;
  let want = [];
  let pingTimer = null;
  let healthTimer = null; // armed/reset per health frame — the hard deadline
  let reconnectTimer = null;
  let socketDead = false;
  let lostReason = null; // reason we tore the socket down ourselves
  let stopped = false;

  function sendWant() {
    if (!open || !ws) return;
    gen += 1;
    try {
      ws.send(JSON.stringify({ action: "want", gen, symbols: [...want] }));
    } catch {
      /* socket dying — close/error path handles it */
    }
  }

  function sameSet(a, b) {
    if (a.length !== b.length) return false;
    const sa = [...a].sort();
    const sb = [...b].sort();
    return sa.every((s, i) => s === sb[i]);
  }

  function setWant(symbols) {
    const next = Array.isArray(symbols) ? symbols : [];
    if (sameSet(next, want)) return;
    want = next;
    sendWant();
  }

  function markDown(reason) {
    if (!healthy) return;
    healthy = false;
    onDown(reason);
  }

  function armHealthDeadline() {
    clearHealthDeadline();
    healthTimer = setTimeout(() => {
      healthTimer = null;
      markDown("health_deadline");
    }, healthDeadlineMs);
  }

  function clearHealthDeadline() {
    if (healthTimer) {
      clearTimeout(healthTimer);
      healthTimer = null;
    }
  }

  function handleSocketLost(reason) {
    if (socketDead) return;
    socketDead = true;
    const wasOpen = open;
    open = false;
    ws = null;
    stopPing();
    clearHealthDeadline();
    if (wasOpen) onSocket(false);
    healthy = false;
    onDown(lostReason ?? reason);
    lostReason = null;
    if (!stopped) {
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, reconnectMs);
    }
  }

  function onMessage(raw) {
    let msg;
    try {
      msg = JSON.parse(
        typeof raw === "string" ? raw : raw.toString("utf8"),
      );
    } catch {
      return;
    }
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "assign") {
      // Assigns are valid only for the latest want gen sent on this socket —
      // the service issues several assigns per gen as partitions move.
      if (Number(msg.gen) !== gen) return;
      onAssign(msg);
    } else if (msg.type === "batch") {
      if (msg.updates && typeof msg.updates === "object") onBatch(msg.updates);
    } else if (msg.type === "health") {
      armHealthDeadline(); // every valid health frame re-arms the deadline
      if (msg.opend_connected === false) {
        markDown("opend_lost");
      } else {
        const recovered = !healthy;
        healthy = true;
        if (recovered && want.length) sendWant(); // re-arm after a stall
      }
      onHealth(msg);
    }
  }

  function tick() {
    const sock = ws;
    if (!sock || !open) return;
    const now = Date.now();
    if (now - lastPongAt > pongTimeoutMs) {
      log(`futu-quotes pong timeout after ${Math.round((now - lastPongAt) / 1000)}s`);
      lostReason = "pong_timeout";
      try {
        sock.terminate ? sock.terminate() : sock.close();
      } catch {
        handleSocketLost("pong_timeout");
      }
      return; // the close event runs handleSocketLost
    }
    try {
      sock.ping();
    } catch {
      /* ignore */
    }
  }

  function startPing() {
    stopPing();
    pingTimer = setInterval(tick, pingIntervalMs);
  }

  function stopPing() {
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
  }

  function connect() {
    if (stopped) return;
    socketDead = false;
    gen = 0; // each connection is an epoch with its own gen counter
    lastPongAt = Date.now();
    const sock = new WebSocketImpl(url);
    ws = sock;
    sock.on("open", () => {
      if (ws !== sock || stopped) return;
      open = true;
      socketDead = false;
      if (want.length) sendWant(); // re-arm on reconnect; fresh epoch starts empty
      onSocket(true);
      startPing();
      armHealthDeadline(); // grace window for the first health frame
    });
    sock.on("pong", () => {
      lastPongAt = Date.now();
    });
    // Messages from a stale (replaced) socket are dropped.
    sock.on("message", (raw) => {
      if (ws === sock) onMessage(raw);
    });
    sock.on("close", () => {
      if (ws === sock) handleSocketLost("socket_close");
    });
    sock.on("error", () => {
      if (ws === sock) handleSocketLost("socket_error");
    });
  }

  function start() {
    stopped = false;
    connect();
  }

  function stop() {
    stopped = true;
    stopPing();
    clearHealthDeadline();
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    const sock = ws;
    ws = null;
    open = false;
    try {
      sock?.close();
    } catch {
      /* ignore */
    }
  }

  return { start, stop, setWant, isOpen: () => open, sentGen: () => gen };
}
