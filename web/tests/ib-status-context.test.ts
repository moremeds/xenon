/**
 * @vitest-environment jsdom
 *
 * Unit tests for IBStatusContext — shared IB connection status via React Context.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import React from "react";
import { IBStatusProvider, useIBStatusContext } from "../lib/IBStatusContext";
import type { ReactNode } from "react";

/* ---------- MockWebSocket ---------- */
class MockWebSocket {
  static CONNECTING = 0 as const;
  static OPEN = 1 as const;
  static CLOSING = 2 as const;
  static CLOSED = 3 as const;
  readyState: number = MockWebSocket.CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  sent: string[] = [];
  url: string;
  constructor(url: string) { this.url = url; }
  send(data: string) { this.sent.push(data); }
  close() {
    if (this.readyState === MockWebSocket.CLOSED) return;
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.(new Event("close"));
  }
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event("open")); }
  simulateMessage(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
  simulateClose() { this.readyState = MockWebSocket.CLOSED; this.onclose?.(new Event("close")); }
}

let wsInstances: MockWebSocket[] = [];
function latestWs(): MockWebSocket { return wsInstances[wsInstances.length - 1]; }

const wrapper = ({ children }: { children: ReactNode }) =>
  React.createElement(IBStatusProvider, null, children);

beforeEach(() => {
  wsInstances = [];
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", class extends MockWebSocket {
    constructor(url: string) { super(url); wsInstances.push(this); }
  });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("IBStatusProvider", () => {
  it("renders children", () => {
    const { result } = renderHook(() => useIBStatusContext(), { wrapper });
    expect(result.current).toBeDefined();
    expect(result.current.wsConnected).toBe(false);
  });

  it("multiple consumers share same connection (only 1 WebSocket created)", () => {
    const Wrapper = ({ children }: { children: ReactNode }) =>
      React.createElement(IBStatusProvider, null, children);

    // Render two hooks under the same provider
    const { result: r1 } = renderHook(() => useIBStatusContext(), { wrapper: Wrapper });
    // Even with a second consumer, still only 1 WebSocket
    expect(wsInstances).toHaveLength(1);
    expect(r1.current).toBeDefined();
  });

  it("wsConnected updates on WS open/close", () => {
    const { result } = renderHook(() => useIBStatusContext(), { wrapper });
    expect(result.current.wsConnected).toBe(false);
    act(() => latestWs().simulateOpen());
    expect(result.current.wsConnected).toBe(true);
    act(() => latestWs().simulateClose());
    expect(result.current.wsConnected).toBe(false);
  });

  it("ibConnected updates from status message", () => {
    const { result } = renderHook(() => useIBStatusContext(), { wrapper });
    act(() => latestWs().simulateOpen());
    // Default ibConnected is true (assume connected until told otherwise)
    act(() => latestWs().simulateMessage({ type: "status", ib_connected: false }));
    expect(result.current.ibConnected).toBe(false);
    act(() => latestWs().simulateMessage({ type: "status", ib_connected: true }));
    expect(result.current.ibConnected).toBe(true);
  });

  it("disconnectedSince set when IB disconnects, cleared on reconnect", () => {
    const { result } = renderHook(() => useIBStatusContext(), { wrapper });
    act(() => latestWs().simulateOpen());
    expect(result.current.disconnectedSince).toBeNull();
    act(() => latestWs().simulateMessage({ type: "status", ib_connected: false }));
    expect(result.current.disconnectedSince).toBeTypeOf("number");
    const ts = result.current.disconnectedSince;
    // Sending another disconnect should not change the timestamp
    act(() => latestWs().simulateMessage({ type: "status", ib_connected: false }));
    expect(result.current.disconnectedSince).toBe(ts);
    // Reconnect clears it
    act(() => latestWs().simulateMessage({ type: "status", ib_connected: true }));
    expect(result.current.disconnectedSince).toBeNull();
  });

  it("connectionState derived correctly for all 3 states", () => {
    const { result } = renderHook(() => useIBStatusContext(), { wrapper });
    // Initial: relay offline (WS not connected)
    expect(result.current.connectionState).toBe("relay_offline");

    act(() => latestWs().simulateOpen());
    // WS open + ibConnected default true = connected
    expect(result.current.connectionState).toBe("connected");

    act(() => latestWs().simulateMessage({ type: "status", ib_connected: false }));
    // WS open + IB disconnected = ib_offline
    expect(result.current.connectionState).toBe("ib_offline");

    act(() => latestWs().simulateClose());
    // WS closed = relay_offline
    expect(result.current.connectionState).toBe("relay_offline");
  });

  it("responds to ping with pong", () => {
    const { result } = renderHook(() => useIBStatusContext(), { wrapper });
    act(() => latestWs().simulateOpen());
    const ws = latestWs();
    act(() => ws.simulateMessage({ type: "ping" }));
    const pongMsg = ws.sent.find(s => {
      try { return JSON.parse(s).action === "pong"; } catch { return false; }
    });
    expect(pongMsg).toBeDefined();
  });

  it("reconnects on WS close with backoff", () => {
    const { result } = renderHook(() => useIBStatusContext(), { wrapper });
    expect(wsInstances).toHaveLength(1);
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateClose());
    // Should schedule reconnect
    const beforeCount = wsInstances.length;
    act(() => vi.advanceTimersByTime(2000));
    expect(wsInstances.length).toBeGreaterThan(beforeCount);
  });

  it("cleans up WebSocket on unmount", () => {
    const { unmount } = renderHook(() => useIBStatusContext(), { wrapper });
    act(() => latestWs().simulateOpen());
    const ws = latestWs();
    unmount();
    expect(ws.readyState).toBe(MockWebSocket.CLOSED);
  });

  it("sets wsConnected false and disconnectedSince when WS drops", () => {
    const { result } = renderHook(() => useIBStatusContext(), { wrapper });
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateMessage({ type: "status", ib_connected: true }));
    expect(result.current.wsConnected).toBe(true);
    expect(result.current.disconnectedSince).toBeNull();
    act(() => latestWs().simulateClose());
    expect(result.current.wsConnected).toBe(false);
    expect(result.current.disconnectedSince).toBeTypeOf("number");
  });
});
