import { describe, it, expect, vi, afterEach } from "vitest";
import {
  latestClosedTradingDayET,
  buildCtaCacheMeta,
} from "../lib/ctaFreshness";

describe("latestClosedTradingDayET", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns today if after market close on a weekday", () => {
    // Wednesday 2026-04-08 at 17:00 ET
    vi.useFakeTimers();
    // 17:00 ET = 21:00 UTC (EDT, UTC-4)
    vi.setSystemTime(new Date("2026-04-08T21:00:00Z"));
    const result = latestClosedTradingDayET(new Date());
    expect(result).toBe("2026-04-08");
  });

  it("returns previous trading day before market close", () => {
    // Wednesday 2026-04-08 at 10:00 ET
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-08T14:00:00Z"));
    const result = latestClosedTradingDayET(new Date());
    expect(result).toBe("2026-04-07"); // Tuesday
  });

  it("returns Friday for Saturday", () => {
    // Saturday 2026-04-11
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-11T15:00:00Z"));
    const result = latestClosedTradingDayET(new Date());
    expect(result).toBe("2026-04-10"); // Friday
  });

  it("returns Friday for Sunday", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-12T15:00:00Z"));
    const result = latestClosedTradingDayET(new Date());
    expect(result).toBe("2026-04-10");
  });
});

describe("buildCtaCacheMeta", () => {
  it("returns fresh when cache date matches target", () => {
    const meta = buildCtaCacheMeta({
      targetDate: "2026-04-08",
      latestCacheDate: "2026-04-08",
      mtimeMs: Date.now() - 60_000,
    });
    expect(meta.is_stale).toBe(false);
    expect(meta.stale_reason).toBe("fresh");
  });

  it("returns behind_target when cache date is old", () => {
    const meta = buildCtaCacheMeta({
      targetDate: "2026-04-08",
      latestCacheDate: "2026-04-07",
      mtimeMs: Date.now() - 86_400_000,
    });
    expect(meta.is_stale).toBe(true);
    expect(meta.stale_reason).toBe("behind_target");
  });

  it("returns missing_cache when no cache exists", () => {
    const meta = buildCtaCacheMeta({
      targetDate: "2026-04-08",
      latestCacheDate: null,
      mtimeMs: null,
    });
    expect(meta.is_stale).toBe(true);
    expect(meta.stale_reason).toBe("missing_cache");
    expect(meta.age_seconds).toBe(null);
    expect(meta.last_refresh).toBe(null);
  });
});
