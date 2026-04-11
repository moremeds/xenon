// @vitest-environment jsdom
import { describe, it, expect, afterEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { useMarketHours, MarketState } from "@/lib/useMarketHours";

/**
 * Boundary tests for the OPEN / EXTENDED / CLOSED classification.
 *
 * The critical property (silly-humming-tide.md plan §4 + review fix #10):
 * frontend and backend must agree on the 16:00 ET boundary using the
 * half-open interval `[9:30, 16:00)`. Any drift at exactly 16:00 causes
 * the frontend to poll a gated backend for one full minute before the
 * next tick flips it to EXTENDED.
 *
 * We test by stubbing `Date` so the hook's internal `new Date()` produces
 * a known ET timestamp. The hook converts via `toLocaleString` which we
 * intercept via a real Date whose hour/minute match the target ET time.
 */

function mockEt(
  hours: number,
  minutes: number,
  { day = 2 }: { day?: number } = {},
) {
  // Construct a UTC date that renders as the target ET wall-clock time.
  // ET is UTC-4 (EDT) or UTC-5 (EST). For these tests we only care that
  // the computed `et.getHours()*60 + et.getMinutes()` matches our target
  // after toLocaleString("en-US", { timeZone: "America/New_York" }).
  //
  // Easiest path: use a fixed `Date` whose UTC time is `hours+4` → ET
  // will be `hours` in EDT. Pick a weekday (Tuesday = day index 2).
  //
  // `day` follows Date.getDay(): 0=Sun, 1=Mon, 2=Tue, ..., 6=Sat.
  // 2026-04-14 is a Tuesday; offset from there.
  const base = new Date(Date.UTC(2026, 3, 14)); // 2026-04-14 Tue
  base.setUTCDate(base.getUTCDate() + (day - 2));
  base.setUTCHours(hours + 4, minutes, 0, 0);
  return base;
}

describe("useMarketHours boundary", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("returns OPEN at 09:30 ET on a weekday", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(mockEt(9, 30));
    const { result } = renderHook(() => useMarketHours());
    await waitFor(() => expect(result.current).toBe(MarketState.OPEN));
  });

  it("returns OPEN at 15:59 ET (one minute before close)", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(mockEt(15, 59));
    const { result } = renderHook(() => useMarketHours());
    await waitFor(() => expect(result.current).toBe(MarketState.OPEN));
  });

  it("returns EXTENDED at exactly 16:00 ET (backend agrees via half-open interval)", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(mockEt(16, 0));
    const { result } = renderHook(() => useMarketHours());
    // Half-open interval [9:30, 16:00) → 16:00 is NOT OPEN.
    // Backend `utils.market_hours.is_market_open` uses `< MARKET_CLOSE`,
    // so the two layers agree at this sharp boundary. Fix #10 regression.
    await waitFor(() => expect(result.current).toBe(MarketState.EXTENDED));
  });

  it("returns EXTENDED at 20:00 ET (last minute of after-hours)", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(mockEt(20, 0));
    const { result } = renderHook(() => useMarketHours());
    await waitFor(() => expect(result.current).toBe(MarketState.EXTENDED));
  });

  it("returns CLOSED at 20:01 ET (past after-hours)", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(mockEt(20, 1));
    const { result } = renderHook(() => useMarketHours());
    await waitFor(() => expect(result.current).toBe(MarketState.CLOSED));
  });

  it("returns CLOSED on Saturday regardless of hour", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(mockEt(12, 0, { day: 6 }));
    const { result } = renderHook(() => useMarketHours());
    await waitFor(() => expect(result.current).toBe(MarketState.CLOSED));
  });
});
