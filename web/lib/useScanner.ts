"use client";

import { useMemo } from "react";
import { useSyncHook, type UseSyncReturn } from "./useSyncHook";
import type { ScannerData } from "./types";

const config = {
  endpoint: "/api/scanner",
  extractTimestamp: (d: ScannerData) => d.scan_time || null,
};

export function useScanner(active: boolean): UseSyncReturn<ScannerData> {
  const stableConfig = useMemo(() => config, []);
  return useSyncHook<ScannerData>(stableConfig, active);
}
