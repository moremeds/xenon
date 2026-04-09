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

const FIXTURE = {
  fetched_at: "2026-04-08T14:00:00Z",
  market_state: "closed" as const,
  ttl_seconds: 300,
  tickers: [],
  action_items: [],
};

function mockFetchOk(body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response);
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

  it("fetches /api/uw-analyze/portfolio on mount and surfaces data", async () => {
    const fetchMock = mockFetchOk(FIXTURE);
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useUwPortfolio());

    await waitFor(() => {
      expect(result.current.data).not.toBeNull();
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/uw-analyze/portfolio",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result.current.data?.fetched_at).toBe(FIXTURE.fetched_at);
    expect(result.current.error).toBeNull();
    expect(result.current.lastFetchedAt).toBe(FIXTURE.fetched_at);
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
    const fetchMock = mockFetchOk(FIXTURE);
    vi.stubGlobal("fetch", fetchMock);

    // First mount populates the module-level cache.
    const first = renderHook(() => useUwPortfolio());
    await waitFor(() => expect(first.result.current.data).not.toBeNull());
    first.unmount();

    // Second mount should see the previous data immediately on the very
    // first render — before any new fetch resolves. Mock fetch with a
    // never-resolving promise so the test can ONLY pass if the cache hit
    // populated initial state.
    const stalled = vi.fn(
      () => new Promise(() => {}) as unknown as Promise<Response>,
    );
    vi.stubGlobal("fetch", stalled);

    const second = renderHook(() => useUwPortfolio());
    expect(second.result.current.data?.fetched_at).toBe(FIXTURE.fetched_at);
    expect(second.result.current.lastFetchedAt).toBe(FIXTURE.fetched_at);
  });

  it("refreshAll() POSTs to /api/uw-analyze/refresh then refetches portfolio", async () => {
    const fetchMock = vi
      .fn()
      // initial mount fetch
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => FIXTURE,
        text: async () => "",
      })
      // refreshAll POST
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({}),
        text: async () => "",
      })
      // follow-up portfolio GET
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ...FIXTURE, fetched_at: "2026-04-08T14:05:00Z" }),
        text: async () => "",
      });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useUwPortfolio());
    await waitFor(() => expect(result.current.data).not.toBeNull());

    await act(async () => {
      await result.current.refreshAll();
    });

    const urls = fetchMock.mock.calls.map((c) => c[0]);
    expect(urls).toContain("/api/uw-analyze/refresh");
    const refreshCall = fetchMock.mock.calls.find(
      (c) => c[0] === "/api/uw-analyze/refresh",
    );
    expect(refreshCall?.[1]?.method).toBe("POST");
    expect(result.current.data?.fetched_at).toBe("2026-04-08T14:05:00Z");
  });
});
