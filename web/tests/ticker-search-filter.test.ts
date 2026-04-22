/**
 * @vitest-environment jsdom
 *
 * Unit tests for TickerSearch secType filtering.
 * Verifies STK, IND, and FUT pass through; other types are filtered out.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  act,
  fireEvent,
  cleanup,
} from "@testing-library/react";
import React from "react";

// TickerSearch's WS connect path awaits resolveBrowserIbRealtimeWsUrl(), which
// normally does a fetch('/api/ib/ws-config'). Under jsdom + fake timers that
// fetch never resolves, so `new WebSocket(...)` never runs and latestWs() is
// undefined. Stub the resolver to a sync URL so the WS is created synchronously
// (after a single microtask flush) in every test.
vi.mock("@/lib/ibRealtimeWsClient", () => ({
  resolveBrowserIbRealtimeWsUrl: async () => "ws://localhost:8765",
}));

import TickerSearch from "../components/TickerSearch";

async function flushMicrotasks() {
  // Two awaits drain the resolver's promise chain + the WS constructor call.
  await Promise.resolve();
  await Promise.resolve();
}

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
  constructor(url: string) {
    this.url = url;
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    if (this.readyState === MockWebSocket.CLOSED) return;
    this.readyState = MockWebSocket.CLOSED;
    // Don't fire onclose to prevent reconnect loops in test cleanup
  }
  simulateOpen() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }
  simulateMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

let wsInstances: MockWebSocket[] = [];
function latestWs(): MockWebSocket {
  return wsInstances[wsInstances.length - 1];
}

beforeEach(() => {
  wsInstances = [];
  vi.useFakeTimers();
  vi.stubGlobal(
    "WebSocket",
    class extends MockWebSocket {
      constructor(url: string) {
        super(url);
        wsInstances.push(this);
      }
    },
  );
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function makeSearchResults(
  results: Array<{ symbol: string; secType: string }>,
) {
  return {
    type: "searchResults",
    results: results.map((r, i) => ({
      conId: 1000 + i,
      symbol: r.symbol,
      secType: r.secType,
      primaryExchange: "SMART",
      currency: "USD",
    })),
  };
}

describe("TickerSearch secType filter", () => {
  it("STK results pass through filter", async () => {
    const onSelect = vi.fn();
    render(React.createElement(TickerSearch, { onSelect }));
    await act(async () => {
      await flushMicrotasks();
    });
    const ws = latestWs();
    act(() => ws.simulateOpen());

    const input = screen.getByRole("combobox");
    act(() => {
      fireEvent.change(input, { target: { value: "AAPL" } });
    });
    act(() => vi.advanceTimersByTime(300));
    act(() =>
      ws.simulateMessage(
        makeSearchResults([{ symbol: "AAPL", secType: "STK" }]),
      ),
    );

    expect(screen.getByText("AAPL")).toBeDefined();
  });

  it("IND results pass through filter", async () => {
    const onSelect = vi.fn();
    render(React.createElement(TickerSearch, { onSelect }));
    await act(async () => {
      await flushMicrotasks();
    });
    const ws = latestWs();
    act(() => ws.simulateOpen());

    const input = screen.getByRole("combobox");
    act(() => {
      fireEvent.change(input, { target: { value: "SPX" } });
    });
    act(() => vi.advanceTimersByTime(300));
    act(() =>
      ws.simulateMessage(
        makeSearchResults([{ symbol: "SPX", secType: "IND" }]),
      ),
    );

    expect(screen.getByText("SPX")).toBeDefined();
  });

  it("FUT results pass through filter", async () => {
    const onSelect = vi.fn();
    render(React.createElement(TickerSearch, { onSelect }));
    await act(async () => {
      await flushMicrotasks();
    });
    const ws = latestWs();
    act(() => ws.simulateOpen());

    const input = screen.getByRole("combobox");
    act(() => {
      fireEvent.change(input, { target: { value: "ES" } });
    });
    act(() => vi.advanceTimersByTime(300));
    act(() =>
      ws.simulateMessage(makeSearchResults([{ symbol: "ES", secType: "FUT" }])),
    );

    expect(screen.getByText("ES")).toBeDefined();
  });

  it("WAR and BOND are filtered out", async () => {
    const onSelect = vi.fn();
    render(React.createElement(TickerSearch, { onSelect }));
    await act(async () => {
      await flushMicrotasks();
    });
    const ws = latestWs();
    act(() => ws.simulateOpen());

    const input = screen.getByRole("combobox");
    act(() => {
      fireEvent.change(input, { target: { value: "TEST" } });
    });
    act(() => vi.advanceTimersByTime(300));
    act(() =>
      ws.simulateMessage(
        makeSearchResults([
          { symbol: "AAPL", secType: "STK" },
          { symbol: "WARR", secType: "WAR" },
          { symbol: "BOND1", secType: "BOND" },
          { symbol: "SPX", secType: "IND" },
        ]),
      ),
    );

    // STK and IND should be visible
    expect(screen.getByText("AAPL")).toBeDefined();
    expect(screen.getByText("SPX")).toBeDefined();
    // WAR and BOND should not be in the DOM
    expect(screen.queryByText("WARR")).toBeNull();
    expect(screen.queryByText("BOND1")).toBeNull();
  });
});
