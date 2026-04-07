"use client";

import { useMemo } from "react";
import { useSyncHook, type UseSyncReturn } from "./useSyncHook";
import type { FlowAnalysisData } from "./types";

export type FlowAccount = "ib" | "futu";

export function useFlowAnalysis(
  activeAccount: FlowAccount,
  active: boolean,
): UseSyncReturn<FlowAnalysisData> {
  const config = useMemo(
    () => ({
      endpoint: `/api/flow-analysis?account=${activeAccount}`,
      extractTimestamp: (d: FlowAnalysisData) => d.analysis_time || null,
    }),
    [activeAccount],
  );
  return useSyncHook<FlowAnalysisData>(config, active);
}
