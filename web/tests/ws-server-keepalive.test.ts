import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * Tests for server-side keep-alive ping/pong behavior.
 *
 * Since ib_realtime_server.js is not importable as a module, we test the
 * behavioral logic by extracting the keep-alive helpers into testable functions
 * that mirror the server implementation.
 */

// Mirror the server's keep-alive constants
const PING_INTERVAL_MS = 30_000;
const PONG_TIMEOUT_MS = 65_000; // 30s * 2 + 5s grace

/**
 * Simulates the server's keep-alive logic:
 * - Tracks lastPongAt per client
 * - Sends pings at interval
 * - Closes clients that haven't ponged within timeout
 */
class KeepAliveManager {
  clientLastPong = new Map<string, number>();
  closedClients: string[] = [];
  sentPings: string[] = [];
  private _timer: ReturnType<typeof setInterval> | null = null;

  addClient(clientId: string) {
    this.clientLastPong.set(clientId, Date.now());
  }

  removeClient(clientId: string) {
    this.clientLastPong.delete(clientId);
  }

  handlePong(clientId: string) {
    if (this.clientLastPong.has(clientId)) {
      this.clientLastPong.set(clientId, Date.now());
    }
  }

  startPingInterval() {
    this._timer = setInterval(() => {
      this._pingAndCheck();
    }, PING_INTERVAL_MS);
  }

  stopPingInterval() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  _pingAndCheck() {
    const now = Date.now();
    for (const [clientId, lastPong] of this.clientLastPong) {
      this.sentPings.push(clientId);
      if (now - lastPong > PONG_TIMEOUT_MS) {
        this.closedClients.push(clientId);
        this.clientLastPong.delete(clientId);
      }
    }
  }
}

describe("WebSocket Keep-Alive Ping/Pong", () => {
  let manager: KeepAliveManager;

  beforeEach(() => {
    vi.useFakeTimers();
    manager = new KeepAliveManager();
  });

  afterEach(() => {
    manager.stopPingInterval();
    vi.useRealTimers();
  });

  it("sends ping to connected clients at 30s interval", () => {
    manager.addClient("c1");
    manager.addClient("c2");
    manager.startPingInterval();

    vi.advanceTimersByTime(PING_INTERVAL_MS);
    expect(manager.sentPings).toContain("c1");
    expect(manager.sentPings).toContain("c2");
  });

  it("pong response updates lastPongAt timestamp", () => {
    manager.addClient("c1");
    const initialTime = Date.now();
    expect(manager.clientLastPong.get("c1")).toBe(initialTime);

    vi.advanceTimersByTime(10_000);
    manager.handlePong("c1");
    expect(manager.clientLastPong.get("c1")).toBe(initialTime + 10_000);
  });

  it("closes client after 2 missed pongs (>65s without pong)", () => {
    manager.addClient("c1");
    manager.startPingInterval();

    // Advance past the pong timeout (65s = 30s * 2 + 5s grace)
    vi.advanceTimersByTime(PONG_TIMEOUT_MS + PING_INTERVAL_MS);

    expect(manager.closedClients).toContain("c1");
    expect(manager.clientLastPong.has("c1")).toBe(false);
  });

  it("does not close client that responds with pong", () => {
    manager.addClient("c1");
    manager.startPingInterval();

    // First ping at 30s — respond with pong
    vi.advanceTimersByTime(PING_INTERVAL_MS);
    manager.handlePong("c1");

    // Second ping at 60s — respond with pong
    vi.advanceTimersByTime(PING_INTERVAL_MS);
    manager.handlePong("c1");

    // Third ping at 90s — respond with pong
    vi.advanceTimersByTime(PING_INTERVAL_MS);
    manager.handlePong("c1");

    expect(manager.closedClients).toEqual([]);
    expect(manager.clientLastPong.has("c1")).toBe(true);
  });

  it("removeClient cleans up tracking", () => {
    manager.addClient("c1");
    expect(manager.clientLastPong.has("c1")).toBe(true);
    manager.removeClient("c1");
    expect(manager.clientLastPong.has("c1")).toBe(false);
  });

  it("selectively closes only unresponsive clients", () => {
    manager.addClient("responsive");
    manager.addClient("dead");
    manager.startPingInterval();

    // Advance 30s — first ping
    vi.advanceTimersByTime(PING_INTERVAL_MS);
    manager.handlePong("responsive"); // only responsive replies

    // Advance another 30s — second ping
    vi.advanceTimersByTime(PING_INTERVAL_MS);
    manager.handlePong("responsive");

    // Advance past timeout for "dead" client
    vi.advanceTimersByTime(PING_INTERVAL_MS);

    expect(manager.closedClients).toContain("dead");
    expect(manager.closedClients).not.toContain("responsive");
  });
});
