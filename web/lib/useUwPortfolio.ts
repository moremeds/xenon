"use client";

/**
 * useUwPortfolio — polling hook for the UW Analyze portfolio dashboard.
 *
 * - Fetches /api/uw-analyze/portfolio as an SSE stream so tickers render
 *   incrementally (cached entries appear near-instantly, fresh scans trickle in).
 * - Falls back to JSON fetch when streaming is unavailable.
 * - Polls every 2 min (open) / 5 min (closed).
 * - Pauses polling when document is hidden (visibilitychange).
 * - Exposes refreshAll(), refreshOne(ticker), addAdhoc(ticker).
 *
 * Backend contract: scripts/api/routes/uw_analyze.py /portfolio + /refresh.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  UwPortfolioResponse,
  UwTickerRow,
  UwActionItem,
} from "@/lib/uwAnalyzeTypes";
import { useMarketHours, MarketState } from "@/lib/useMarketHours";

const POLL_OPEN_MS = 2 * 60 * 1000;
const POLL_CLOSED_MS = 5 * 60 * 1000;

// Module-level cache: survives unmount/remount of components using this
// hook within the same browser session.
type CacheSnapshot = {
  data: UwPortfolioResponse;
  lastFetchedAt: string | null;
};
let _cachedSnapshot: CacheSnapshot | null = null;

/** Test-only: clear the cross-mount snapshot cache. */
export function __resetUwPortfolioCacheForTests(): void {
  _cachedSnapshot = null;
}

export type UseUwPortfolioState = {
  data: UwPortfolioResponse | null;
  loading: boolean;
  error: string | null;
  lastFetchedAt: string | null;
  refreshAll: () => Promise<void>;
  refreshOne: (ticker: string) => Promise<void>;
  addAdhoc: (ticker: string) => Promise<void>;
};

// ---------------------------------------------------------------------------
// SSE streaming fetch
// ---------------------------------------------------------------------------

type SseCallbacks = {
  onMeta: (meta: {
    fetched_at: string;
    market_state: "open" | "closed";
    ttl_seconds: number;
  }) => void;
  onTicker: (row: UwTickerRow) => void;
  onDone: (summary: { action_items: UwActionItem[] }) => void;
};

async function _fetchPortfolioStreaming(
  callbacks: SseCallbacks,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/uw-analyze/portfolio", {
    cache: "no-store",
    headers: { Accept: "text/event-stream" },
    signal,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`portfolio fetch ${res.status}: ${body.slice(0, 200)}`);
  }
  if (!res.body) throw new Error("no response body for SSE stream");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Parse complete SSE events (delimited by blank lines).
      const events = buffer.split(/\n\n|\r\n\r\n/);
      buffer = events.pop()!; // incomplete last chunk stays in buffer

      for (const event of events) {
        if (!event.trim()) continue;
        const lines = event.split(/\r?\n/);
        let eventType = "message";
        let data = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) eventType = line.slice(7).trim();
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (!data) continue;
        try {
          const parsed = JSON.parse(data);
          if (eventType === "meta") callbacks.onMeta(parsed);
          else if (eventType === "done") callbacks.onDone(parsed);
          else callbacks.onTicker(parsed);
        } catch {
          // Malformed JSON in an SSE event — skip it.
        }
      }
    }
    // Flush decoder for any trailing bytes.
    buffer += decoder.decode();
  } finally {
    reader.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// JSON fetch (fallback, used by refreshAll/refreshOne after POST)
// ---------------------------------------------------------------------------

async function _fetchPortfolioJson(): Promise<UwPortfolioResponse> {
  const res = await fetch("/api/uw-analyze/portfolio", { cache: "no-store" });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`portfolio fetch ${res.status}: ${body.slice(0, 200)}`);
  }
  return res.json();
}

async function _postRefresh(body: {
  tickers?: string[];
  adhoc?: boolean;
}): Promise<void> {
  const res = await fetch("/api/uw-analyze/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`refresh ${res.status}: ${txt.slice(0, 200)}`);
  }
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useUwPortfolio(): UseUwPortfolioState {
  const [data, setData] = useState<UwPortfolioResponse | null>(
    _cachedSnapshot?.data ?? null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastFetchedAt, setLastFetchedAt] = useState<string | null>(
    _cachedSnapshot?.lastFetchedAt ?? null,
  );

  const inFlight = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const market = useMarketHours();

  const fetchPortfolio = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;

    // Cancel any previous in-flight stream.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setError(null);

    try {
      let meta: Partial<UwPortfolioResponse> = {};
      const tickers: UwTickerRow[] = [];
      let actionItems: UwActionItem[] = [];

      await _fetchPortfolioStreaming(
        {
          onMeta: (m) => {
            meta = m;
          },
          onTicker: (row) => {
            tickers.push(row);
            // Incremental state update — each ticker paints immediately.
            setData((prev) => ({
              ...(prev ?? {
                fetched_at: "",
                market_state: "closed" as const,
                ttl_seconds: 300,
                tickers: [],
                action_items: [],
              }),
              ...meta,
              tickers: [...tickers],
            }));
          },
          onDone: (summary) => {
            actionItems = summary.action_items;
            setData((prev) =>
              prev ? { ...prev, action_items: summary.action_items } : prev,
            );
          },
        },
        controller.signal,
      );

      const stamp =
        (meta as { fetched_at?: string }).fetched_at ??
        new Date().toISOString();
      setLastFetchedAt(stamp);
      _cachedSnapshot = {
        data: {
          ...meta,
          tickers,
          action_items: actionItems,
        } as UwPortfolioResponse,
        lastFetchedAt: stamp,
      };
    } catch (e: unknown) {
      // AbortError from superseding fetch — not a real error.
      if (e instanceof Error && e.name === "AbortError") return;
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
      inFlight.current = false;
    }
  }, []);

  // Initial fetch + interval.
  useEffect(() => {
    fetchPortfolio();
    const interval =
      market === MarketState.OPEN ? POLL_OPEN_MS : POLL_CLOSED_MS;
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        fetchPortfolio();
      }
    }, interval);
    const onVis = () => {
      if (document.visibilityState === "visible") fetchPortfolio();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
      abortRef.current?.abort();
    };
  }, [fetchPortfolio, market]);

  const refreshAll = useCallback(async () => {
    setLoading(true);
    try {
      await _postRefresh({});
      await fetchPortfolio();
    } catch (e: unknown) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, [fetchPortfolio]);

  const refreshOne = useCallback(
    async (ticker: string) => {
      const t = (ticker ?? "").trim().toUpperCase();
      if (!t) return;
      try {
        await _postRefresh({ tickers: [t] });
        await fetchPortfolio();
      } catch (e: unknown) {
        setError(String(e instanceof Error ? e.message : e));
      }
    },
    [fetchPortfolio],
  );

  const addAdhoc = useCallback(
    async (ticker: string) => {
      const t = (ticker ?? "").trim().toUpperCase();
      if (!t) return;
      try {
        await _postRefresh({ tickers: [t], adhoc: true });
        await fetchPortfolio();
      } catch (e: unknown) {
        setError(String(e instanceof Error ? e.message : e));
      }
    },
    [fetchPortfolio],
  );

  return {
    data,
    loading,
    error,
    lastFetchedAt,
    refreshAll,
    refreshOne,
    addAdhoc,
  };
}
