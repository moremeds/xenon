"use client";

import { useEffect, useState } from "react";

export async function fetchIbConnectedFromHealth(
  signal?: AbortSignal,
): Promise<boolean | null> {
  try {
    const res = await fetch("/api/health", { cache: "no-store", signal });
    if (!res.ok) return null;
    const data = (await res.json()) as {
      ib_pool?: Record<string, { connected?: boolean }>;
    };
    const pool = data?.ib_pool;
    if (pool && typeof pool === "object") {
      return Object.values(pool).some(
        (role): role is { connected: true } =>
          !!role &&
          typeof role === "object" &&
          (role as { connected?: unknown }).connected === true,
      );
    }
    return null;
  } catch {
    return null;
  }
}

export function useIbHealthFallback(
  active: boolean,
  intervalMs = 15_000,
): boolean | null {
  const [ibConnected, setIbConnected] = useState<boolean | null>(null);

  useEffect(() => {
    if (!active) return;

    let cancelled = false;
    const controller = new AbortController();
    const poll = async () => {
      const reading = await fetchIbConnectedFromHealth(controller.signal);
      if (!cancelled && reading !== null) setIbConnected(reading);
    };
    void poll();
    const id = setInterval(() => void poll(), intervalMs);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(id);
    };
  }, [active, intervalMs]);

  return ibConnected;
}
