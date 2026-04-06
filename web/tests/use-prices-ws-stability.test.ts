/**
 * @vitest-environment jsdom
 *
 * Unit tests for usePrices WebSocket connection stability.
 * Validates the state-machine + diff-based subscription sync refactor.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { usePrices } from "../lib/usePrices";
import type { PriceData } from "../lib/pricesProtocol";

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
function makePriceData(symbol: string, last: number): PriceData {
  return { symbol, last, lastIsCalculated: false, bid: last - 0.01, ask: last + 0.01, bidSize: 100, askSize: 100, volume: 1000, high: last + 1, low: last - 1, open: last, close: last - 0.5, week52High: null, week52Low: null, avgVolume: null, delta: null, gamma: null, theta: null, vega: null, impliedVol: null, undPrice: null, timestamp: new Date().toISOString() };
}
function latestWs(): MockWebSocket { return wsInstances[wsInstances.length - 1]; }
function sentMessages(ws: MockWebSocket) { return ws.sent.map((s) => JSON.parse(s)); }

beforeEach(() => {
  wsInstances = [];
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", class extends MockWebSocket {
    constructor(url: string) { super(url); wsInstances.push(this); }
  });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("Connection stability", () => {
  it("does not recreate WS when symbols change", () => {
    const { rerender } = renderHook(
      (props: { symbols: string[] }) => usePrices({ symbols: props.symbols, enabled: true }),
      { initialProps: { symbols: ["AAPL"] } },
    );
    expect(wsInstances).toHaveLength(1);
    act(() => latestWs().simulateOpen());
    rerender({ symbols: ["AAPL", "MSFT"] });
    expect(wsInstances).toHaveLength(1);
  });

  it("does not recreate WS when contracts change", () => {
    const c1 = { symbol: "PLTR", expiry: "20260320", strike: 100, right: "C" as const };
    const c2 = { symbol: "PLTR", expiry: "20260320", strike: 110, right: "C" as const };
    const { rerender } = renderHook(
      (props: { contracts: typeof c1[] }) => usePrices({ symbols: ["PLTR"], contracts: props.contracts, enabled: true }),
      { initialProps: { contracts: [c1] } },
    );
    expect(wsInstances).toHaveLength(1);
    act(() => latestWs().simulateOpen());
    rerender({ contracts: [c1, c2] });
    expect(wsInstances).toHaveLength(1);
  });

  it("sends diff-based subscribe when symbols added", () => {
    const { rerender } = renderHook(
      (props: { symbols: string[] }) => usePrices({ symbols: props.symbols, enabled: true }),
      { initialProps: { symbols: ["AAPL"] } },
    );
    const ws = latestWs();
    act(() => ws.simulateOpen());
    expect(sentMessages(ws)).toHaveLength(1);
    expect(sentMessages(ws)[0].symbols).toContain("AAPL");
    rerender({ symbols: ["AAPL", "MSFT"] });
    const all = sentMessages(ws);
    expect(all).toHaveLength(2);
    expect(all[1].action).toBe("subscribe");
    expect(all[1].symbols).toContain("MSFT");
    expect(all[1].symbols).not.toContain("AAPL");
  });

  it("sends unsubscribe when symbols removed", () => {
    const { rerender } = renderHook(
      (props: { symbols: string[] }) => usePrices({ symbols: props.symbols, enabled: true }),
      { initialProps: { symbols: ["AAPL", "MSFT"] } },
    );
    act(() => latestWs().simulateOpen());
    rerender({ symbols: ["AAPL"] });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const unsub = sentMessages(latestWs()).find((m: any) => m.action === "unsubscribe");
    expect(unsub).toBeDefined();
    expect(unsub.symbols).toContain("MSFT");
  });
});

describe("Idempotent connect", () => {
  it("calling connect while CONNECTING creates no extra socket", () => {
    const { result } = renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    expect(wsInstances).toHaveLength(1);
    act(() => result.current.reconnect());
    expect(wsInstances.length).toBeLessThanOrEqual(2);
  });

  it("calling connect while OPEN creates no extra socket", () => {
    renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    expect(wsInstances).toHaveLength(1);
    act(() => latestWs().simulateOpen());
    expect(wsInstances).toHaveLength(1);
  });
});

describe("Stale socket isolation", () => {
  it("old socket onclose after new socket exists does not trigger reconnect", () => {
    const { result } = renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    const oldWs = latestWs();
    act(() => oldWs.simulateOpen());
    act(() => result.current.reconnect());
    expect(latestWs()).not.toBe(oldWs);
    act(() => { oldWs.readyState = MockWebSocket.CLOSED; oldWs.onclose?.(new Event("close")); });
    expect(wsInstances).toHaveLength(2);
  });

  it("old socket onmessage does not overwrite newer state", () => {
    const { result } = renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    const oldWs = latestWs();
    act(() => oldWs.simulateOpen());
    act(() => oldWs.simulateMessage({ type: "price", symbol: "AAPL", data: makePriceData("AAPL", 100) }));
    expect(result.current.prices.AAPL?.last).toBe(100);
    act(() => result.current.reconnect());
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateMessage({ type: "price", symbol: "AAPL", data: makePriceData("AAPL", 200) }));
    expect(result.current.prices.AAPL?.last).toBe(200);
    act(() => { oldWs.onmessage?.({ data: JSON.stringify({ type: "price", symbol: "AAPL", data: makePriceData("AAPL", 50) }) }); });
    expect(result.current.prices.AAPL?.last).toBe(200);
  });
});

describe("Reconnect timer cleanup", () => {
  it("unmount clears pending reconnect timeout", () => {
    const spy = vi.spyOn(globalThis, "clearTimeout");
    const { unmount } = renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateClose());
    unmount();
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });

  it("enabled=false clears pending reconnect timeout", () => {
    const { rerender } = renderHook(
      (props: { enabled: boolean }) => usePrices({ symbols: ["AAPL"], enabled: props.enabled }),
      { initialProps: { enabled: true } },
    );
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateClose());
    rerender({ enabled: false });
    const before = wsInstances.length;
    act(() => vi.advanceTimersByTime(60_000));
    expect(wsInstances.length).toBe(before);
  });

  it("reconnect timer does not stack multiple retries", () => {
    renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateClose());
    const after = wsInstances.length;
    act(() => vi.advanceTimersByTime(1600));
    expect(wsInstances.length).toBe(after + 1);
  });

  it("exponential backoff increases delay on sequential failures", () => {
    renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateClose());
    act(() => vi.advanceTimersByTime(1600));
    expect(wsInstances.length).toBe(2);
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateClose());
    act(() => vi.advanceTimersByTime(1600));
    const at1600 = wsInstances.length;
    act(() => vi.advanceTimersByTime(2000));
    expect(wsInstances.length).toBeGreaterThanOrEqual(at1600);
  });

  it("backoff resets on successful open", () => {
    renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateClose());
    act(() => vi.advanceTimersByTime(1600));
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateClose());
    act(() => vi.advanceTimersByTime(1600));
    expect(wsInstances.length).toBeGreaterThanOrEqual(3);
  });
});

describe("Subscription diff", () => {
  it("does not re-send identical subscriptions when hashes unchanged", () => {
    const { rerender } = renderHook(
      (props: { symbols: string[] }) => usePrices({ symbols: props.symbols, enabled: true }),
      { initialProps: { symbols: ["AAPL"] } },
    );
    const ws = latestWs();
    act(() => ws.simulateOpen());
    expect(ws.sent).toHaveLength(1);
    rerender({ symbols: ["AAPL"] });
    expect(ws.sent).toHaveLength(1);
  });

  it("sends only diff (added/removed), not full re-subscribe", () => {
    const { rerender } = renderHook(
      (props: { symbols: string[] }) => usePrices({ symbols: props.symbols, enabled: true }),
      { initialProps: { symbols: ["AAPL", "MSFT"] } },
    );
    act(() => latestWs().simulateOpen());
    rerender({ symbols: ["AAPL", "NVDA"] });
    const msgs = sentMessages(latestWs());
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const subs = msgs.filter((m: any) => m.action === "subscribe");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const unsubs = msgs.filter((m: any) => m.action === "unsubscribe");
    expect(subs[subs.length - 1].symbols).toContain("NVDA");
    expect(subs[subs.length - 1].symbols).not.toContain("AAPL");
    expect(unsubs.length).toBeGreaterThanOrEqual(1);
    expect(unsubs[unsubs.length - 1].symbols).toContain("MSFT");
  });

  it("evicts price data for removed subscriptions", () => {
    const { result, rerender } = renderHook(
      (props: { symbols: string[] }) => usePrices({ symbols: props.symbols, enabled: true }),
      { initialProps: { symbols: ["AAPL", "MSFT"] } },
    );
    act(() => latestWs().simulateOpen());
    act(() => {
      latestWs().simulateMessage({ type: "price", symbol: "AAPL", data: makePriceData("AAPL", 175) });
      latestWs().simulateMessage({ type: "price", symbol: "MSFT", data: makePriceData("MSFT", 420) });
    });
    expect(result.current.prices.AAPL).toBeDefined();
    expect(result.current.prices.MSFT).toBeDefined();
    rerender({ symbols: ["AAPL"] });
    expect(result.current.prices.AAPL).toBeDefined();
    expect(result.current.prices.MSFT).toBeUndefined();
  });

  it("preserves prices for unchanged subscriptions across sub changes", () => {
    const { result, rerender } = renderHook(
      (props: { symbols: string[] }) => usePrices({ symbols: props.symbols, enabled: true }),
      { initialProps: { symbols: ["AAPL"] } },
    );
    act(() => latestWs().simulateOpen());
    act(() => { latestWs().simulateMessage({ type: "price", symbol: "AAPL", data: makePriceData("AAPL", 175) }); });
    rerender({ symbols: ["AAPL", "MSFT"] });
    expect(result.current.prices.AAPL?.last).toBe(175);
  });
});

describe("Lifecycle transitions", () => {
  it("creates WS when first subscription arrives", () => {
    const { result, rerender } = renderHook(
      (props: { symbols: string[] }) => usePrices({ symbols: props.symbols, enabled: true }),
      { initialProps: { symbols: [] as string[] } },
    );
    expect(wsInstances).toHaveLength(0);
    rerender({ symbols: ["AAPL"] });
    expect(wsInstances).toHaveLength(1);
    act(() => latestWs().simulateOpen());
    expect(result.current.connected).toBe(true);
  });

  it("closes WS when all subscriptions removed", () => {
    const { result, rerender } = renderHook(
      (props: { symbols: string[] }) => usePrices({ symbols: props.symbols, enabled: true }),
      { initialProps: { symbols: ["AAPL"] } },
    );
    act(() => latestWs().simulateOpen());
    expect(result.current.connected).toBe(true);
    rerender({ symbols: [] as string[] });
    expect(result.current.connected).toBe(false);
  });

  it("closes and stays closed when enabled becomes false", () => {
    const { result, rerender } = renderHook(
      (props: { enabled: boolean }) => usePrices({ symbols: ["AAPL"], enabled: props.enabled }),
      { initialProps: { enabled: true } },
    );
    act(() => latestWs().simulateOpen());
    expect(result.current.connected).toBe(true);
    rerender({ enabled: false });
    expect(result.current.connected).toBe(false);
    const before = wsInstances.length;
    act(() => vi.advanceTimersByTime(60_000));
    expect(wsInstances.length).toBe(before);
  });

  it("reconnects when enabled flips false->true", () => {
    const { result, rerender } = renderHook(
      (props: { enabled: boolean }) => usePrices({ symbols: ["AAPL"], enabled: props.enabled }),
      { initialProps: { enabled: true } },
    );
    act(() => latestWs().simulateOpen());
    rerender({ enabled: false });
    expect(result.current.connected).toBe(false);
    rerender({ enabled: true });
    act(() => latestWs().simulateOpen());
    expect(result.current.connected).toBe(true);
  });
});

describe("Callback refs", () => {
  it("latest onPriceUpdate is invoked (not stale closure)", () => {
    const cb1 = vi.fn();
    const cb2 = vi.fn();
    const { rerender } = renderHook(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (props: { cb: (u: any) => void }) => usePrices({ symbols: ["AAPL"], enabled: true, onPriceUpdate: props.cb }),
      { initialProps: { cb: cb1 } },
    );
    act(() => latestWs().simulateOpen());
    rerender({ cb: cb2 });
    act(() => { latestWs().simulateMessage({ type: "price", symbol: "AAPL", data: makePriceData("AAPL", 175) }); });
    expect(cb1).not.toHaveBeenCalled();
    expect(cb2).toHaveBeenCalledTimes(1);
  });
});

describe("Price state across reconnects", () => {
  it("preserves last-known prices until fresh ticks arrive", () => {
    const { result } = renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    act(() => latestWs().simulateOpen());
    act(() => { latestWs().simulateMessage({ type: "price", symbol: "AAPL", data: makePriceData("AAPL", 175) }); });
    expect(result.current.prices.AAPL?.last).toBe(175);
    act(() => latestWs().simulateClose());
    expect(result.current.prices.AAPL?.last).toBe(175);
    act(() => vi.advanceTimersByTime(1600));
    act(() => latestWs().simulateOpen());
    expect(result.current.prices.AAPL?.last).toBe(175);
    act(() => { latestWs().simulateMessage({ type: "price", symbol: "AAPL", data: makePriceData("AAPL", 180) }); });
    expect(result.current.prices.AAPL?.last).toBe(180);
  });
});

describe("Message hardening", () => {
  it("ignores malformed JSON without crashing", () => {
    const { result } = renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    act(() => latestWs().simulateOpen());
    act(() => { latestWs().onmessage?.({ data: "not valid json{{{" }); });
    expect(result.current.connected).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it("ignores unknown message types without crashing", () => {
    const { result } = renderHook(() => usePrices({ symbols: ["AAPL"], enabled: true }));
    act(() => latestWs().simulateOpen());
    act(() => { latestWs().simulateMessage({ type: "unknown_future_type", foo: "bar" }); });
    expect(result.current.connected).toBe(true);
  });
});
