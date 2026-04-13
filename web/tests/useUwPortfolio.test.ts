// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";

// Keep market-hours deterministic. Default OPEN so the SSE codepath runs —
// under the closed-market gate (silly-humming-tide.md plan §4), non-OPEN
// states skip SSE entirely. Individual tests override this when they need
// to exercise closed-market behavior.
let _mockedMarketState: "open" | "extended" | "closed" = "open";
vi.mock("@/lib/useMarketHours", () => ({
  MarketState: { OPEN: "open", EXTENDED: "extended", CLOSED: "closed" },
  useMarketHours: () => _mockedMarketState,
}));
function setMockedMarketState(state: "open" | "extended" | "closed"): void {
  _mockedMarketState = state;
}

import {
  useUwPortfolio,
  __resetUwPortfolioCacheForTests,
} from "@/lib/useUwPortfolio";

const FIXTURE_META = {
  fetched_at: "2026-04-08T14:00:00Z",
  market_state: "closed",
  ttl_seconds: 300,
};

const FIXTURE_DONE = {
  action_items: [],
};

/** Build an SSE text payload from events. */
function buildSse(events: { type?: string; data: unknown }[]): string {
  return events
    .map((e) => {
      const prefix = e.type ? `event: ${e.type}\n` : "";
      return `${prefix}data: ${JSON.stringify(e.data)}\n\n`;
    })
    .join("");
}

/** Create a mock fetch Response that streams SSE text. */
function mockSseResponse(sseText: string): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(sseText));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function mockFetchSse(sseText: string) {
  return vi.fn().mockImplementation((url: string) => {
    if (typeof url === "string" && url.includes("/uw-analyze/portfolio")) {
      return Promise.resolve(mockSseResponse(sseText));
    }
    // Fallback for POST /refresh etc.
    return Promise.resolve(
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });
}

describe("useUwPortfolio", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    __resetUwPortfolioCacheForTests();
    setMockedMarketState("open"); // default OPEN so SSE path runs
  });
  afterEach(() => {
    vi.restoreAllMocks();
    __resetUwPortfolioCacheForTests();
    setMockedMarketState("open");
  });

  it("fetches /api/uw-analyze/portfolio via SSE and surfaces data", async () => {
    const sse = buildSse([
      { type: "meta", data: FIXTURE_META },
      {
        data: {
          ticker: "SPY",
          sources: [],
          snapshot: {},
          changes: [],
          snapshot_ts: FIXTURE_META.fetched_at,
        },
      },
      { type: "done", data: FIXTURE_DONE },
    ]);
    const fetchMock = mockFetchSse(sse);
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useUwPortfolio());

    await waitFor(() => {
      expect(result.current.data).not.toBeNull();
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/uw-analyze/portfolio",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result.current.data?.tickers).toHaveLength(1);
    expect(result.current.data?.tickers[0].ticker).toBe("SPY");
    expect(result.current.error).toBeNull();
    expect(result.current.lastFetchedAt).toBe(FIXTURE_META.fetched_at);
  });

  it("surfaces error state when the portfolio fetch rejects", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("network down"));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useUwPortfolio());

    await waitFor(() => {
      expect(result.current.error).toBeTruthy();
    });
    expect(result.current.error).toContain("network down");
    expect(result.current.data).toBeNull();
  });

  it("re-mount paints the previous snapshot immediately from the module cache", async () => {
    const sse = buildSse([
      { type: "meta", data: FIXTURE_META },
      {
        data: {
          ticker: "SPY",
          sources: [],
          snapshot: {},
          changes: [],
          snapshot_ts: FIXTURE_META.fetched_at,
        },
      },
      { type: "done", data: FIXTURE_DONE },
    ]);
    const fetchMock = mockFetchSse(sse);
    vi.stubGlobal("fetch", fetchMock);

    // First mount populates the module-level cache.
    const first = renderHook(() => useUwPortfolio());
    await waitFor(() => expect(first.result.current.data).not.toBeNull());
    first.unmount();

    // Second mount should see the previous data immediately on the very
    // first render. Mock fetch with a never-resolving promise so the test
    // can ONLY pass if the cache hit populated initial state.
    const stalled = vi.fn(
      () => new Promise(() => {}) as unknown as Promise<Response>,
    );
    vi.stubGlobal("fetch", stalled);

    const second = renderHook(() => useUwPortfolio());
    expect(second.result.current.data?.tickers).toHaveLength(1);
    expect(second.result.current.lastFetchedAt).toBe(FIXTURE_META.fetched_at);
  });

  it("refreshAll() POSTs to /api/uw-analyze/refresh then refetches portfolio", async () => {
    const sse1 = buildSse([
      { type: "meta", data: FIXTURE_META },
      {
        data: {
          ticker: "SPY",
          sources: [],
          snapshot: {},
          changes: [],
          snapshot_ts: FIXTURE_META.fetched_at,
        },
      },
      { type: "done", data: FIXTURE_DONE },
    ]);
    const sse2 = buildSse([
      {
        type: "meta",
        data: { ...FIXTURE_META, fetched_at: "2026-04-08T14:05:00Z" },
      },
      {
        data: {
          ticker: "SPY",
          sources: [],
          snapshot: {},
          changes: [],
          snapshot_ts: FIXTURE_META.fetched_at,
        },
      },
      {
        data: {
          ticker: "QQQ",
          sources: [],
          snapshot: {},
          changes: [],
          snapshot_ts: "2026-04-08T14:05:00Z",
        },
      },
      { type: "done", data: FIXTURE_DONE },
    ]);
    let callCount = 0;
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === "string" && url.includes("/uw-analyze/portfolio")) {
        callCount++;
        const text = callCount <= 1 ? sse1 : sse2;
        return Promise.resolve(mockSseResponse(text));
      }
      // POST /refresh
      return Promise.resolve(
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useUwPortfolio());
    await waitFor(() => expect(result.current.data).not.toBeNull());

    await act(async () => {
      await result.current.refreshAll();
    });

    const urls = fetchMock.mock.calls.map((c: unknown[]) => c[0]);
    expect(urls).toContain("/api/uw-analyze/refresh");
    const refreshCall = fetchMock.mock.calls.find(
      (c: unknown[]) => c[0] === "/api/uw-analyze/refresh",
    );
    expect(refreshCall?.[1]?.method).toBe("POST");
    expect(result.current.data?.tickers).toHaveLength(2);
  });

  // ── Closed-market gate (silly-humming-tide.md plan §4) ──────────────

  it("does NOT run SSE stream when market is CLOSED", async () => {
    setMockedMarketState("closed");
    const cachedPayload = {
      fetched_at: "2026-04-08T03:00:00Z",
      market_state: "closed",
      ttl_seconds: 1800,
      closed_market_paused: true,
      tickers: [
        {
          ticker: "SPY",
          sources: [],
          snapshot: {},
          changes: [],
          has_snapshot: true,
          served_stale: false,
        },
      ],
      action_items: [],
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === "string" && url.includes("?cached=true")) {
        return Promise.resolve(
          new Response(JSON.stringify(cachedPayload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      // Any non-cached portfolio GET must NOT be called — fail loudly.
      if (typeof url === "string" && url.includes("/uw-analyze/portfolio")) {
        throw new Error("closed-market must not fire SSE fetch");
      }
      return Promise.resolve(
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useUwPortfolio());
    await waitFor(() => {
      expect(result.current.data).not.toBeNull();
    });

    // Prefetch fired, surfaced the cached payload.
    expect(result.current.data?.tickers).toHaveLength(1);
    // Only the cache-only endpoint was hit.
    const urls = fetchMock.mock.calls.map((c: unknown[]) => c[0] as string);
    expect(
      urls.every(
        (u) =>
          u.includes("?cached=true") || !u.includes("/uw-analyze/portfolio"),
      ),
    ).toBe(true);
  });

  it("refreshAll() still works when market is CLOSED (bypasses gate)", async () => {
    setMockedMarketState("closed");
    const cachedPayload = {
      fetched_at: null,
      market_state: "closed",
      ttl_seconds: 1800,
      closed_market_paused: true,
      tickers: [],
      action_items: [],
    };
    const sseAfterRefresh = buildSse([
      {
        type: "meta",
        data: { ...FIXTURE_META, market_state: "closed" },
      },
      {
        data: {
          ticker: "SPY",
          sources: [],
          snapshot: {},
          changes: [],
          has_snapshot: true,
          served_stale: false,
        },
      },
      { type: "done", data: FIXTURE_DONE },
    ]);
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === "string" && url.includes("?cached=true")) {
        return Promise.resolve(
          new Response(JSON.stringify(cachedPayload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (typeof url === "string" && url.includes("/uw-analyze/portfolio")) {
        // After refreshAll(), the hook pulls SSE — allowed because it's the
        // user-initiated path chained by refreshAll.
        return Promise.resolve(mockSseResponse(sseAfterRefresh));
      }
      // POST /refresh
      return Promise.resolve(
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useUwPortfolio());
    // Wait for initial prefetch to settle.
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.refreshAll();
    });

    const urls = fetchMock.mock.calls.map((c: unknown[]) => c[0] as string);
    expect(urls).toContain("/api/uw-analyze/refresh");
    const refreshCall = fetchMock.mock.calls.find(
      (c: unknown[]) => c[0] === "/api/uw-analyze/refresh",
    );
    expect(refreshCall?.[1]?.method).toBe("POST");
    // SSE GET /portfolio fired after the POST with ?user_initiated=1 so
    // the backend bypasses BOTH the cache gate AND the OI-fetch gate
    // (silly-humming-tide.md review fix #1).
    expect(
      urls.some((u) => u === "/api/uw-analyze/portfolio?user_initiated=1"),
    ).toBe(true);
    // Fresh tickers arrived.
    expect(result.current.data?.tickers).toHaveLength(1);
  });

  it("does NOT run SSE stream when market is EXTENDED (treat same as CLOSED)", async () => {
    // Silly-humming-tide.md review fix #10. EXTENDED hours must pause
    // auto-polling just like CLOSED — otherwise the frontend polls the
    // backend only for `is_market_open()` to return False and gate
    // everything, wasting HTTP round trips.
    setMockedMarketState("extended");
    const cachedPayload = {
      fetched_at: "2026-04-08T08:00:00Z",
      market_state: "closed",
      ttl_seconds: 1800,
      closed_market_paused: true,
      tickers: [
        {
          ticker: "SPY",
          sources: [],
          snapshot: {},
          changes: [],
          has_snapshot: true,
          served_stale: false,
        },
      ],
      action_items: [],
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === "string" && url.includes("?cached=true")) {
        return Promise.resolve(
          new Response(JSON.stringify(cachedPayload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      // Any non-cached portfolio GET during EXTENDED must not fire.
      if (typeof url === "string" && url.includes("/uw-analyze/portfolio")) {
        throw new Error("extended-hours must not fire SSE fetch");
      }
      return Promise.resolve(
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useUwPortfolio());
    await waitFor(() => expect(result.current.data).not.toBeNull());

    // Only the cache-only endpoint was hit — no SSE during extended hours.
    const urls = fetchMock.mock.calls.map((c: unknown[]) => c[0] as string);
    expect(
      urls.every(
        (u) =>
          u.includes("?cached=true") || !u.includes("/uw-analyze/portfolio"),
      ),
    ).toBe(true);
    // Prefetched data still paints the tile.
    expect(result.current.data?.tickers).toHaveLength(1);
  });

  // ── Two-Map merge: cached tickers stay visible during SSE ─────────

  it("cached tickers remain visible while SSE tickers stream in (monotonicity)", async () => {
    // Cache prefetch returns 3 tickers (A, B, C).
    const cachedPayload = {
      fetched_at: "2026-04-08T14:00:00Z",
      market_state: "open",
      ttl_seconds: 300,
      tickers: [
        {
          ticker: "A",
          sources: [],
          snapshot: {},
          changes: [],
          snapshot_ts: "2026-04-08T14:00:00Z",
        },
        {
          ticker: "B",
          sources: [],
          snapshot: {},
          changes: [],
          snapshot_ts: "2026-04-08T14:00:00Z",
        },
        {
          ticker: "C",
          sources: [],
          snapshot: {},
          changes: [],
          snapshot_ts: "2026-04-08T14:00:00Z",
        },
      ],
      action_items: [],
    };

    // SSE streams in 2 separate chunks: first A, then B + done.
    // This lets us observe the intermediate state after chunk 1.
    const chunk1 = buildSse([
      { type: "meta", data: FIXTURE_META },
      {
        data: {
          ticker: "A",
          sources: ["sse"],
          snapshot: { updated: true },
          changes: [],
          snapshot_ts: "2026-04-08T14:05:00Z",
        },
      },
    ]);
    const chunk2 = buildSse([
      {
        data: {
          ticker: "B",
          sources: ["sse"],
          snapshot: { updated: true },
          changes: [],
          snapshot_ts: "2026-04-08T14:05:00Z",
        },
      },
      { type: "done", data: FIXTURE_DONE },
    ]);

    // Gated ReadableStream: chunk2 is held until the test explicitly
    // releases it, giving us a stable window to assert intermediate state.
    let releaseChunk2!: () => void;
    const chunk2Gate = new Promise<void>((r) => {
      releaseChunk2 = r;
    });

    function mockGatedSseResponse(): Response {
      const encoder = new TextEncoder();
      const stream = new ReadableStream({
        async start(controller) {
          controller.enqueue(encoder.encode(chunk1));
          await chunk2Gate;
          controller.enqueue(encoder.encode(chunk2));
          controller.close();
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }

    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === "string" && url.includes("?cached=true")) {
        return Promise.resolve(
          new Response(JSON.stringify(cachedPayload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (typeof url === "string" && url.includes("/uw-analyze/portfolio")) {
        return Promise.resolve(mockGatedSseResponse());
      }
      return Promise.resolve(
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useUwPortfolio());

    // After chunk1 (meta + ticker A via SSE), all 3 should still be visible.
    // BUG (pre-fix): the old code shows only [A] — cache wiped.
    // Chunk2 is gated, so we have a stable window to check.
    await waitFor(() => {
      const tickers = result.current.data?.tickers ?? [];
      const hasSseA = tickers.some(
        (t) => t.ticker === "A" && t.sources?.[0] === "sse",
      );
      expect(hasSseA).toBe(true);
      // Monotonicity: cached B+C still visible alongside SSE A.
      expect(tickers.length).toBeGreaterThanOrEqual(3);
    });

    // Release chunk2 (ticker B + done).
    releaseChunk2();

    // Wait for stream to finish.
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    // After SSE completes, only SSE tickers remain (C dropped).
    const finalTickers = result.current
      .data!.tickers.map((t) => t.ticker)
      .sort();
    expect(finalTickers).toEqual(["A", "B"]);

    // SSE data overwrote cache for A.
    const tickerA = result.current.data!.tickers.find((t) => t.ticker === "A");
    expect(tickerA?.sources).toEqual(["sse"]);
  });
});
