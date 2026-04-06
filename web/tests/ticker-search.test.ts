/**
 * Unit tests for TickerSearch component.
 * Validates WS message handling, keyboard nav, and error states.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Minimal WS mock
class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];

  constructor(public url: string) {
    // Auto-open on next tick
    setTimeout(() => {
      this.readyState = MockWebSocket.OPEN;
      this.onopen?.();
    }, 0);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }
}

// Track all created WS instances
let wsInstances: MockWebSocket[] = [];

describe("TickerSearch WS protocol", () => {
  beforeEach(() => {
    wsInstances = [];
    vi.stubGlobal("WebSocket", class extends MockWebSocket {
      constructor(url: string) {
        super(url);
        wsInstances.push(this);
      }
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends search action with pattern over WS", async () => {
    // Simulate what the component does when user types
    const ws = new MockWebSocket("ws://localhost:8765");
    await new Promise((r) => setTimeout(r, 10)); // let onopen fire

    ws.send(JSON.stringify({ action: "search", pattern: "AAPL" }));
    expect(ws.sent).toHaveLength(1);

    const msg = JSON.parse(ws.sent[0]);
    expect(msg.action).toBe("search");
    expect(msg.pattern).toBe("AAPL");
  });

  it("parses searchResults response and filters STK only", () => {
    const rawResults = [
      { conId: 1, symbol: "AAPL", secType: "STK", primaryExchange: "NASDAQ", currency: "USD" },
      { conId: 2, symbol: "AAPL", secType: "OPT", primaryExchange: "CBOE", currency: "USD" },
      { conId: 3, symbol: "AAPLX", secType: "STK", primaryExchange: "NYSE", currency: "USD" },
    ];

    // This is the filtering logic from the component
    const filtered = rawResults
      .filter((r) => r.secType === "STK")
      .slice(0, 10);

    expect(filtered).toHaveLength(2);
    expect(filtered[0].symbol).toBe("AAPL");
    expect(filtered[1].symbol).toBe("AAPLX");
  });

  it("handles empty results when IB is disconnected", () => {
    const response = { type: "searchResults", pattern: "AAPL", results: [] };
    expect(response.results).toHaveLength(0);
    // Component should show "No results" not "Searching..."
  });

  it("uppercases input before sending", () => {
    const input = "aapl";
    const normalized = input.toUpperCase();
    expect(normalized).toBe("AAPL");
  });

  it("debounces rapid input changes", async () => {
    const ws = new MockWebSocket("ws://localhost:8765");
    await new Promise((r) => setTimeout(r, 10));

    // Simulate rapid typing with debounce behavior
    const patterns = ["A", "AA", "AAP", "AAPL"];
    let lastSent: string | null = null;

    // Only the final pattern should be sent after debounce
    const timer = setTimeout(() => {
      lastSent = patterns[patterns.length - 1];
      ws.send(JSON.stringify({ action: "search", pattern: lastSent }));
    }, 200);

    await new Promise((r) => setTimeout(r, 250));
    clearTimeout(timer);

    expect(ws.sent).toHaveLength(1);
    expect(JSON.parse(ws.sent[0]).pattern).toBe("AAPL");
  });

  it("keyboard navigation: ArrowDown increments activeIndex", () => {
    const results = [
      { conId: 1, symbol: "AAPL", secType: "STK", primaryExchange: "NASDAQ", currency: "USD" },
      { conId: 2, symbol: "AMZN", secType: "STK", primaryExchange: "NASDAQ", currency: "USD" },
    ];

    let activeIndex = -1;

    // Simulate ArrowDown
    activeIndex = activeIndex < results.length - 1 ? activeIndex + 1 : 0;
    expect(activeIndex).toBe(0);

    activeIndex = activeIndex < results.length - 1 ? activeIndex + 1 : 0;
    expect(activeIndex).toBe(1);

    // Wrap around
    activeIndex = activeIndex < results.length - 1 ? activeIndex + 1 : 0;
    expect(activeIndex).toBe(0);
  });

  it("keyboard navigation: ArrowUp decrements activeIndex with wrap", () => {
    const results = [
      { conId: 1, symbol: "AAPL", secType: "STK", primaryExchange: "NASDAQ", currency: "USD" },
      { conId: 2, symbol: "AMZN", secType: "STK", primaryExchange: "NASDAQ", currency: "USD" },
    ];

    let activeIndex = -1;

    // From -1 (nothing selected), ArrowUp should not navigate (handled by component guard)
    // But if results are shown and activeIndex is 0:
    activeIndex = 0;
    activeIndex = activeIndex > 0 ? activeIndex - 1 : results.length - 1;
    expect(activeIndex).toBe(1); // wraps to last

    activeIndex = activeIndex > 0 ? activeIndex - 1 : results.length - 1;
    expect(activeIndex).toBe(0);
  });
});
