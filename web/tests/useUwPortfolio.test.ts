// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";

// Keep market-hours deterministic so polling interval is known.
vi.mock("@/lib/useMarketHours", () => ({
  MarketState: { OPEN: "open", EXTENDED: "extended", CLOSED: "closed" },
  useMarketHours: () => "closed",
}));

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
  });
  afterEach(() => {
    vi.restoreAllMocks();
    __resetUwPortfolioCacheForTests();
  });

  it("fetches /api/uw-analyze/portfolio via SSE and surfaces data", async () => {
    const sse = buildSse([
      { type: "meta", data: FIXTURE_META },
      { data: { ticker: "SPY", sources: [], snapshot: {}, changes: [] } },
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
      { data: { ticker: "SPY", sources: [], snapshot: {}, changes: [] } },
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
      { data: { ticker: "SPY", sources: [], snapshot: {}, changes: [] } },
      { type: "done", data: FIXTURE_DONE },
    ]);
    const sse2 = buildSse([
      {
        type: "meta",
        data: { ...FIXTURE_META, fetched_at: "2026-04-08T14:05:00Z" },
      },
      { data: { ticker: "SPY", sources: [], snapshot: {}, changes: [] } },
      { data: { ticker: "QQQ", sources: [], snapshot: {}, changes: [] } },
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
});
