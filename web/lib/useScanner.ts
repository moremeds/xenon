"use client";

import { useMemo } from "react";
import { useSyncHook, type UseSyncReturn } from "./useSyncHook";
import type { ScannerData } from "./types";

const config = {
  endpoint: "/api/scanner",
  extractTimestamp: (d: ScannerData) => d.scan_timestamp || null,
  hasPost: false,
};

export function useScanner(active: boolean): UseSyncReturn<ScannerData> {
  const stableConfig = useMemo(() => config, []);
  return useSyncHook<ScannerData>(stableConfig, active);
}
