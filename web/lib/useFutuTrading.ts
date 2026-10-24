"use client";

import { useEffect, useState } from "react";
import type { FutuAccountEnv } from "@/lib/futuAccountEnv";

export type FutuTradingStatus = {
  paper: boolean;
  live: boolean;
};

function parseFutuTrading(body: unknown): FutuTradingStatus | null {
  if (!body || typeof body !== "object") return null;
  const futu = (body as { futu?: unknown }).futu;
  if (!futu || typeof futu !== "object") return null;
  const trading = (futu as { trading?: unknown }).trading;
  if (!trading || typeof trading !== "object") return null;
  const paper = (trading as { paper?: unknown }).paper;
  const live = (trading as { live?: unknown }).live;
  if (typeof paper !== "boolean" || typeof live !== "boolean") return null;
  return { paper, live };
}

/** `GET /api/health` → `futu.trading` (SIMULATE/REAL order surfaces). */
export function useFutuTrading(): FutuTradingStatus | null {
  const [status, setStatus] = useState<FutuTradingStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (!res.ok) return;
        const data = await res.json();
        const parsed = parseFutuTrading(data);
        if (!cancelled && parsed) setStatus(parsed);
      } catch {
        // Silent: backend may be down during frontend-only dev sessions.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return status;
}

export function futuTradingEnabledForEnv(
  status: FutuTradingStatus | null,
  env: FutuAccountEnv,
): boolean {
  if (!status) return false;
  return env === "paper" ? status.paper : status.live;
}
