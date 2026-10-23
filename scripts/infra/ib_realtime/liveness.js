// Keep-alive liveness for WS clients. Pure (no IB deps) so it is
// unit-testable with fake timers, like subscriber_registry.
//
// A client counts as alive on ANY of:
//   - app-level {action:"pong"} handled via noteAlive()
//   - a protocol "pong" frame (auto-answer to our ws.ping())
//   - a protocol "ping" frame (client-initiated; ws auto-replies pong)
// Each sweep sends the app {type:"ping"} JSON and a protocol ws.ping().
// Clients silent past PONG_TIMEOUT_MS are closed.

export const PING_INTERVAL_MS = 30_000;
export const PONG_TIMEOUT_MS = 65_000; // 30s * 2 + 5s grace

export function createLiveness({
  clients,
  sendAppPing,
  onAlive,
  closeClient,
  log = () => {},
  pingIntervalMs = PING_INTERVAL_MS,
  pongTimeoutMs = PONG_TIMEOUT_MS,
}) {
  const lastAlive = new Map(); // client -> epoch ms of last sign of life
  let timer = null;

  function noteAlive(client, nowMs = Date.now()) {
    lastAlive.set(client, nowMs);
    onAlive?.(client, nowMs);
  }

  function attach(client, nowMs = Date.now()) {
    lastAlive.set(client, nowMs);
    client.on("pong", () => noteAlive(client));
    client.on("ping", () => noteAlive(client));
  }

  function detach(client) {
    lastAlive.delete(client);
  }

  function tick(nowMs = Date.now()) {
    for (const client of clients) {
      sendAppPing(client);
      try {
        client.ping();
      } catch {
        /* ignore */
      }
      const last = lastAlive.get(client);
      if (last !== undefined && nowMs - last > pongTimeoutMs) {
        log(
          `Closing unresponsive client (no pong for ${Math.round((nowMs - last) / 1000)}s)`,
        );
        lastAlive.delete(client);
        try {
          closeClient(client);
        } catch {
          /* ignore */
        }
      }
    }
  }

  function start() {
    timer = setInterval(() => tick(), pingIntervalMs);
    return timer;
  }

  function stop() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  return { attach, detach, noteAlive, tick, start, stop, lastAlive };
}
