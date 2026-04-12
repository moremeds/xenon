/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useUwStatsHistory } from "../lib/useUwStatsHistory";

const MOCK_HISTORY = {
  buckets: [
    {
      hour: "2026-04-08T14:00:00Z",
      requests_2xx: 50,
      requests_4xx: 2,
      requests_5xx: 0,
      cached: 20,
      avg_latency_ms: 180,
    },
    {
      hour: "2026-04-08T15:00:00Z",
      requests_2xx: 60,
      requests_4xx: 1,
      requests_5xx: 1,
      cached: 25,
      avg_latency_ms: 200,
    },
  ],
};

/** Flush pending microtasks (resolved promises) under fake timers. */
const flushMicrotasks = () => act(() => Promise.resolve());

describe("useUwStatsHistory", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(MOCK_HISTORY), { status: 200 }),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("fetches history on mount with default hours=96", async () => {
    const { result } = renderHook(() => useUwStatsHistory());
    await flushMicrotasks();
    expect(result.current).not.toBe(null);
    expect(result.current!.buckets).toHaveLength(2);
    expect(fetch).toHaveBeenCalledWith(
      "/api/uw-stats/history?hours=96",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("passes custom hours parameter", async () => {
    renderHook(() => useUwStatsHistory(24));
    await flushMicrotasks();
    expect(fetch).toHaveBeenCalledWith(
      "/api/uw-stats/history?hours=24",
      expect.anything(),
    );
  });

  it("polls every 60 seconds", async () => {
    const { result } = renderHook(() => useUwStatsHistory());
    await flushMicrotasks();
    expect(result.current).not.toBe(null);

    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ buckets: [] }), { status: 200 }),
    );
    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    await flushMicrotasks();
    expect(result.current!.buckets).toHaveLength(0);
  });

  it("returns null on error (silent fail)", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("offline"));
    const { result } = renderHook(() => useUwStatsHistory());
    await flushMicrotasks();
    expect(result.current).toBe(null);
  });
});
