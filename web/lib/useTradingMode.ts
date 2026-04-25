"use client";

import { useEffect, useState } from "react";

export type TradingMode = "paper" | "live";

export function useTradingMode(): TradingMode | null {
  const [mode, setMode] = useState<TradingMode | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (!res.ok) return;
        const data = (await res.json()) as { trading_mode?: string };
        const value = data.trading_mode;
        if (!cancelled && (value === "paper" || value === "live")) {
          setMode(value);
        }
      } catch {
        // Silent: backend may be down during frontend-only dev sessions.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return mode;
}
