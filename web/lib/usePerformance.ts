"use client";

import { useMemo } from "react";
import { useSyncHook, type UseSyncReturn } from "./useSyncHook";
import type { PerformanceData, PerformancePeriod } from "./types";

/** Hook is broker- AND period-aware: the endpoint URL embeds both, so each
 *  (broker, period) combination keys its own cache entry inside useSyncHook's
 *  module-level map. Switching either swaps cache entries (no stale results). */
export function usePerformance(
  active: boolean,
  broker: "IB" | "FUTU" = "IB",
  period: PerformancePeriod = "YTD",
): UseSyncReturn<PerformanceData> {
  const config = useMemo(
    () => ({
      endpoint: `/api/performance?broker=${encodeURIComponent(broker)}&period=${encodeURIComponent(period)}`,
      interval: 15 * 60 * 1000,
      hasPost: false,
      extractTimestamp: (data: PerformanceData) =>
        data.status === "ok"
          ? (data.last_sync ?? data.as_of ?? null)
          : (data.last_sync ?? null),
    }),
    [broker, period],
  );
  return useSyncHook<PerformanceData>(config, active);
}
