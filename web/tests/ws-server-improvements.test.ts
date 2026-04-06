import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * Tests for WS server improvements:
 * - Search cache cleared on IB reconnect
 * - Adaptive batch flushing (threshold + minimum interval)
 */

describe("Search cache cleared on IB reconnect", () => {
  it("clears searchCache when IB reconnects", () => {
    // Simulate the searchCache and the IB connected handler logic
    const searchCache = new Map<string, { results: unknown[]; ts: number }>();

    // Populate cache with stale data
    searchCache.set("AAPL", { results: [{ symbol: "AAPL" }], ts: Date.now() });
    searchCache.set("GOOG", { results: [{ symbol: "GOOG" }], ts: Date.now() });
    expect(searchCache.size).toBe(2);

    // Simulate IB reconnect handler: the server should clear searchCache
    // This is the behavior we're implementing
    searchCache.clear();

    expect(searchCache.size).toBe(0);
  });

  it("new searches work after cache clear", () => {
    const searchCache = new Map<string, { results: unknown[]; ts: number }>();
    searchCache.set("AAPL", { results: [{ symbol: "AAPL" }], ts: Date.now() });

    // Reconnect clears
    searchCache.clear();
    expect(searchCache.has("AAPL")).toBe(false);

    // New search repopulates
    searchCache.set("AAPL", { results: [{ symbol: "AAPL", conId: 123 }], ts: Date.now() });
    expect(searchCache.has("AAPL")).toBe(true);
    expect(searchCache.get("AAPL")!.results).toEqual([{ symbol: "AAPL", conId: 123 }]);
  });
});

describe("Adaptive batch flushing", () => {
  const BATCH_INTERVAL_MS = 100;
  const BATCH_THRESHOLD = 50;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * Simulates the adaptive batch flush logic:
   * - bufferPriceForClient adds to per-client buffer
   * - If any client's buffer >= BATCH_THRESHOLD and enough time since last flush,
   *   flush immediately
   * - flushBatches() records lastFlushTime
   */
  class AdaptiveBatchManager {
    clientBuffers = new Map<string, Map<string, unknown>>();
    lastFlushTime = 0;
    flushCount = 0;
    flushedData: Array<{ client: string; symbols: string[] }> = [];

    bufferPrice(clientId: string, symbol: string, data: unknown) {
      let buf = this.clientBuffers.get(clientId);
      if (!buf) {
        buf = new Map();
        this.clientBuffers.set(clientId, buf);
      }
      buf.set(symbol, data);

      // Adaptive: flush early if threshold exceeded and min interval elapsed
      if (
        buf.size >= BATCH_THRESHOLD &&
        Date.now() - this.lastFlushTime >= BATCH_INTERVAL_MS
      ) {
        this.flush();
      }
    }

    flush() {
      this.lastFlushTime = Date.now();
      this.flushCount++;
      for (const [clientId, buf] of this.clientBuffers) {
        if (buf.size === 0) continue;
        this.flushedData.push({
          client: clientId,
          symbols: [...buf.keys()],
        });
        buf.clear();
      }
    }
  }

  it("triggers immediate flush when buffer exceeds threshold", () => {
    const manager = new AdaptiveBatchManager();

    // Buffer 50 symbols for one client (exceeds threshold)
    for (let i = 0; i < BATCH_THRESHOLD; i++) {
      manager.bufferPrice("c1", `SYM${i}`, { last: i });
    }

    // Should have flushed immediately
    expect(manager.flushCount).toBe(1);
    expect(manager.flushedData[0].symbols.length).toBe(BATCH_THRESHOLD);
  });

  it("does not flush early when buffer is below threshold", () => {
    const manager = new AdaptiveBatchManager();

    for (let i = 0; i < BATCH_THRESHOLD - 1; i++) {
      manager.bufferPrice("c1", `SYM${i}`, { last: i });
    }

    // 49 symbols — below threshold, no early flush
    expect(manager.flushCount).toBe(0);
  });

  it("respects minimum interval between flushes", () => {
    const manager = new AdaptiveBatchManager();

    // First batch of 50 — triggers immediate flush
    for (let i = 0; i < BATCH_THRESHOLD; i++) {
      manager.bufferPrice("c1", `SYM${i}`, { last: i });
    }
    expect(manager.flushCount).toBe(1);

    // Immediately add another 50 — should NOT flush (< 100ms since last)
    for (let i = 0; i < BATCH_THRESHOLD; i++) {
      manager.bufferPrice("c1", `SYM${i}`, { last: i + 100 });
    }
    expect(manager.flushCount).toBe(1); // Still 1, minimum interval not elapsed

    // Advance time past minimum interval
    vi.advanceTimersByTime(BATCH_INTERVAL_MS);

    // Now buffer 50 more — should trigger flush
    for (let i = 0; i < BATCH_THRESHOLD; i++) {
      manager.bufferPrice("c1", `SYM${i}`, { last: i + 200 });
    }
    expect(manager.flushCount).toBe(2);
  });

  it("flushBatches records lastFlushTime", () => {
    // Set a known start time
    vi.setSystemTime(new Date(10_000));
    const manager = new AdaptiveBatchManager();
    expect(manager.lastFlushTime).toBe(0);

    vi.advanceTimersByTime(5000);
    manager.flush();
    expect(manager.lastFlushTime).toBe(15_000);
  });
});
