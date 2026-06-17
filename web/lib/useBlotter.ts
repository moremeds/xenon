"use client";

import type { BlotterData } from "./types";
import { useSyncHook } from "./useSyncHook";

type UseBlotterReturn = {
  data: BlotterData | null;
  loading: boolean;
  syncing: boolean;
  error: string | null;
  syncNow: () => void;
};

export function useBlotter(
  active = false,
  broker: "IB" | "FUTU" = "IB",
): UseBlotterReturn {
  // Endpoint carries the broker — useSyncHook keys its module cache on the
  // endpoint string, so IB and FUTU blotters stay isolated across tab switches.
  const result = useSyncHook<BlotterData>(
    {
      endpoint: broker === "FUTU" ? "/api/blotter?broker=FUTU" : "/api/blotter",
      extractTimestamp: (data) => data.as_of || null,
      showBackgroundError: true,
    },
    active,
  );

  return {
    data: result.data,
    loading: result.loading,
    syncing: result.syncing,
    error: result.error,
    syncNow: result.syncNow,
  };
}
