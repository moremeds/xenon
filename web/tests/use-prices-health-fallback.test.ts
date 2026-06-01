/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { usePrices } from "@/lib/usePrices";

class DeadWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = DeadWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  constructor() {
    setTimeout(() => {
      this.readyState = DeadWebSocket.CLOSED;
      this.onclose?.();
    }, 0);
  }
  send() {}
  close() {
    this.readyState = DeadWebSocket.CLOSED;
  }
}

beforeEach(() => {
  vi.stubGlobal("WebSocket", DeadWebSocket as unknown as typeof WebSocket);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("/api/health")
        ? {
            ok: true,
            json: async () => ({ ib_pool: { sync: { connected: true } } }),
          }
        : { ok: true, json: async () => ({ url: "ws://localhost:8765" }) },
    ) as unknown as typeof fetch,
  );
});
afterEach(() => vi.restoreAllMocks());

describe("usePrices health fallback", () => {
  it("reports ibConnected=true from /api/health when the WS never opens", async () => {
    const { result } = renderHook(() =>
      usePrices({ symbols: ["AAPL"], contracts: [], indexes: [] }),
    );
    await waitFor(() => expect(result.current.connected).toBe(false));
    await waitFor(() => expect(result.current.ibConnected).toBe(true));
  });

  it("reports ibConnected=false when WS is down AND /api/health reports IB down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        String(url).includes("/api/health")
          ? {
              ok: true,
              json: async () => ({
                ib_pool: {
                  sync: { connected: false },
                  orders: { connected: false },
                  data: { connected: false },
                },
              }),
            }
          : { ok: true, json: async () => ({ url: "ws://localhost:8765" }) },
      ) as unknown as typeof fetch,
    );
    const { result } = renderHook(() =>
      usePrices({ symbols: ["AAPL"], contracts: [], indexes: [] }),
    );
    await waitFor(() => expect(result.current.connected).toBe(false));
    await waitFor(() => expect(result.current.ibConnected).toBe(false));
  });

  it("does not poll /api/health when hook is disabled or has no subscriptions", async () => {
    const fetchSpy = vi.fn(async () => ({
      ok: true,
      json: async () => ({ ib_pool: { sync: { connected: true } } }),
    }));
    vi.stubGlobal("fetch", fetchSpy as unknown as typeof fetch);
    renderHook(() =>
      usePrices({
        symbols: [],
        contracts: [],
        indexes: [],
        enabled: false,
      }),
    );
    await new Promise((r) => setTimeout(r, 50));
    const healthCalls = fetchSpy.mock.calls.filter(([url]) =>
      String(url).includes("/api/health"),
    );
    expect(healthCalls).toHaveLength(0);
  });
});
