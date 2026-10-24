"use client";

import { useMemo } from "react";
import { futuScopeQuery, useFutuAccountEnv } from "./futuAccountEnv";
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
  const futuEnv = useFutuAccountEnv();
  const endpoint = useMemo(
    () =>
      broker === "FUTU"
        ? `/api/blotter${futuScopeQuery(futuEnv)}`
        : "/api/blotter",
    [broker, futuEnv],
  );
  // Endpoint carries broker + Futu account_env — useSyncHook keys cache on it.
  const result = useSyncHook<BlotterData>(
    {
      endpoint,
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
