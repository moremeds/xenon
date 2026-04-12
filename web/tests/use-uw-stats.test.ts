/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useUwStats } from "../lib/useUwStats";

const MOCK_STATS = {
  totals: {
    requests: 100,
    success: 90,
    cached: 30,
    retries: 5,
    failures: 3,
    rate_limits: 2,
    connection_errors: 0,
  },
  latency_ms: { samples: 90, min: 50, max: 800, avg: 200, p95: 500 },
  by_status: { "200": 90, "429": 2, "500": 3 },
  uptime_seconds: 3600,
};

/** Flush pending microtasks (resolved promises) under fake timers. */
const flushMicrotasks = () => act(() => Promise.resolve());

describe("useUwStats", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(MOCK_STATS), { status: 200 }),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("fetches stats on mount", async () => {
    const { result } = renderHook(() => useUwStats());
    // useEffect fires synchronously in act, but fetchStats is async —
    // flush the resolved promise so setState runs.
    await flushMicrotasks();
    expect(result.current).not.toBe(null);
    expect(result.current!.totals.requests).toBe(100);
    expect(result.current!.latency_ms.p95).toBe(500);
  });

  it("polls every 10 seconds", async () => {
    const { result } = renderHook(() => useUwStats());
    await flushMicrotasks();
    expect(result.current).not.toBe(null);

    const updatedStats = {
      ...MOCK_STATS,
      totals: { ...MOCK_STATS.totals, requests: 200 },
    };
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify(updatedStats), { status: 200 }),
    );

    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });
    // Flush the promise from the interval callback
    await flushMicrotasks();
    expect(result.current!.totals.requests).toBe(200);
  });

  it("returns null on fetch error (silent fail)", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("network error"));
    const { result } = renderHook(() => useUwStats());
    await flushMicrotasks();
    // Should remain null — not throw
    expect(result.current).toBe(null);
  });

  it("returns null on non-200 response", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response("", { status: 500 }));
    const { result } = renderHook(() => useUwStats());
    await flushMicrotasks();
    expect(result.current).toBe(null);
  });

  it("cleans up interval on unmount", async () => {
    const { result, unmount } = renderHook(() => useUwStats());
    await flushMicrotasks();
    expect(result.current).not.toBe(null);
    unmount();
    // After unmount, further intervals should not call fetch
    const callCount = vi.mocked(fetch).mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(20_000);
    });
    await flushMicrotasks();
    expect(vi.mocked(fetch).mock.calls.length).toBe(callCount);
  });
});
