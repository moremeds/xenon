"use client";

const DEFAULT_IB_REALTIME_PORT = 8765;

let cachedUrl: string | null = null;
let inflightUrl: Promise<string> | null = null;

function fallbackBrowserWsUrl(): string {
  if (typeof window === "undefined") {
    return `ws://localhost:${DEFAULT_IB_REALTIME_PORT}`;
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:${DEFAULT_IB_REALTIME_PORT}`;
}

export async function resolveBrowserIbRealtimeWsUrl(): Promise<string> {
  if (process.env.NEXT_PUBLIC_IB_REALTIME_WS_URL) {
    return process.env.NEXT_PUBLIC_IB_REALTIME_WS_URL;
  }
  if (cachedUrl) return cachedUrl;

  if (!inflightUrl) {
    inflightUrl = fetch("/api/ib/ws-config", { cache: "no-store" })
      .then(async (res) => {
        if (!res.ok) throw new Error(`ws-config failed: ${res.status}`);
        const data = (await res.json()) as { url?: string };
        return data.url || fallbackBrowserWsUrl();
      })
      .catch(() => fallbackBrowserWsUrl())
      .then((url) => {
        cachedUrl = url;
        return url;
      })
      .finally(() => {
        inflightUrl = null;
      });
  }

  return inflightUrl;
}
