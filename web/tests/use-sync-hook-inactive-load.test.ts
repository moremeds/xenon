/**
 * @vitest-environment jsdom
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useSyncHook } from "../lib/useSyncHook";

type Payload = {
  scan_time: string;
  value: number;
};

function jsonResponse(body: Payload) {
  return {
    ok: true,
    async json() {
      return body;
    },
  };
}

describe("useSyncHook inactive initial load", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("reads cached data once even when inactive", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ scan_time: "2026-03-22T09:00:00Z", value: 7 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      useSyncHook<Payload>(
        {
          endpoint: "/api/internals",
          hasPost: false,
          extractTimestamp: (data) => data.scan_time,
        },
        false,
      ),
    );

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.data?.value).toBe(7);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/internals", { method: "GET" });
  });

  it("triggers the first sync when a previously inactive hook becomes active", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ scan_time: "2026-03-22T09:00:00Z", value: 7 }))
      .mockResolvedValueOnce(jsonResponse({ scan_time: "2026-03-22T09:01:00Z", value: 8 }));
    vi.stubGlobal("fetch", fetchMock);

    const { result, rerender } = renderHook(
      ({ active }: { active: boolean }) =>
        useSyncHook<Payload>(
          {
            endpoint: "/api/internals",
            extractTimestamp: (data) => data.scan_time,
          },
          active,
        ),
      { initialProps: { active: false } },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data?.value).toBe(7);

    rerender({ active: true });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/internals", { method: "POST" });
  });

  it("retries stale cached data even when polling is inactive", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ scan_time: "", value: 0 }))
      .mockResolvedValueOnce(jsonResponse({ scan_time: "2026-03-22T09:01:00Z", value: 8 }));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      useSyncHook<Payload>(
        {
          endpoint: "/api/internals",
          hasPost: false,
          extractTimestamp: (data) => data.scan_time,
          shouldRetry: (data) => !data.scan_time,
          retryIntervalMs: 5000,
          retryMethod: "GET",
        },
        false,
      ),
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.data?.value).toBe(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/internals", { method: "GET" });
    expect(result.current.data?.value).toBe(8);
  });
});
