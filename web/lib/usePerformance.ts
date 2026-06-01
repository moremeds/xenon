"use client";

import { useMemo } from "react";
import { useSyncHook, type UseSyncReturn } from "./useSyncHook";
import type { PerformanceData } from "./types";

/** Hook is broker-aware: each broker has its own endpoint URL and therefore
 *  its own cache entry inside useSyncHook's module-level map. Switching the
 *  account tab swaps which cache entry the panel reads from. */
export function usePerformance(
  active: boolean,
  broker: "IB" | "FUTU" = "IB",
): UseSyncReturn<PerformanceData> {
  const config = useMemo(
    () => ({
      endpoint: `/api/performance?broker=${encodeURIComponent(broker)}`,
      interval: 15 * 60 * 1000,
      hasPost: false,
      extractTimestamp: (data: PerformanceData) =>
        data.status === "ok"
          ? (data.last_sync ?? data.as_of ?? null)
          : (data.last_sync ?? null),
    }),
    [broker],
  );
  return useSyncHook<PerformanceData>(config, active);
}
