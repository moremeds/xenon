/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { renderHook, cleanup } from "@testing-library/react";

afterEach(() => cleanup());

// Mock BEFORE importing usePerformance so the inner import picks up the mock.
vi.mock("@/lib/useSyncHook", () => ({
  useSyncHook: vi.fn(() => ({
    data: null,
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  })),
}));

import { usePerformance } from "@/lib/usePerformance";
import { useSyncHook } from "@/lib/useSyncHook";

describe("usePerformance period parameter", () => {
  it("includes ?broker= and &period= in the endpoint", () => {
    renderHook(() => usePerformance(true, "FUTU", "3M"));
    const config = (useSyncHook as ReturnType<typeof vi.fn>).mock.calls.at(
      -1,
    )![0];
    expect(config.endpoint).toBe("/api/performance?broker=FUTU&period=3M");
  });

  it("defaults to broker=IB and period=YTD when args omitted", () => {
    renderHook(() => usePerformance(true));
    const config = (useSyncHook as ReturnType<typeof vi.fn>).mock.calls.at(
      -1,
    )![0];
    expect(config.endpoint).toBe("/api/performance?broker=IB&period=YTD");
  });

  it("changing the period creates a different endpoint (per-period cache)", () => {
    const { rerender } = renderHook(
      ({ p }: { p: "1M" | "YTD" }) => usePerformance(true, "IB", p),
      { initialProps: { p: "YTD" } },
    );
    const ytdEndpoint = (useSyncHook as ReturnType<typeof vi.fn>).mock.calls.at(
      -1,
    )![0].endpoint;
    rerender({ p: "1M" });
    const oneMonthEndpoint = (
      useSyncHook as ReturnType<typeof vi.fn>
    ).mock.calls.at(-1)![0].endpoint;
    expect(ytdEndpoint).not.toBe(oneMonthEndpoint);
  });
});
