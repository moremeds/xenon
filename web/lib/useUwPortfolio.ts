"use client";

/**
 * useUwPortfolio — polling hook for the UW Analyze portfolio dashboard.
 *
 * - Polls /api/uw-analyze/portfolio every 2 min during market hours,
 *   5 min when closed.
 * - Pauses polling when document is hidden (visibilitychange).
 * - Exposes refreshAll(), refreshOne(ticker), addAdhoc(ticker).
 *
 * Backend contract: scripts/api/routes/uw_analyze.py /portfolio + /refresh.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { UwPortfolioResponse } from "@/lib/uwAnalyzeTypes";
import { useMarketHours, MarketState } from "@/lib/useMarketHours";

const POLL_OPEN_MS = 2 * 60 * 1000;
const POLL_CLOSED_MS = 5 * 60 * 1000;

// Module-level cache: survives unmount/remount of components using this
// hook within the same browser session. Without it, navigating away from
// /uw-analyze and back drops local useState and the page shows empty
// until the next fetch lands. This stash lets re-mount paint the last
// known data immediately while a background revalidation runs.
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

async function _fetchPortfolio(): Promise<UwPortfolioResponse> {
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

export function useUwPortfolio(): UseUwPortfolioState {
  // Initialize from the module-level cache so re-mount paints the last
  // known snapshot immediately. The background fetch in the effect below
  // still runs to revalidate.
  const [data, setData] = useState<UwPortfolioResponse | null>(
    _cachedSnapshot?.data ?? null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastFetchedAt, setLastFetchedAt] = useState<string | null>(
    _cachedSnapshot?.lastFetchedAt ?? null,
  );

  const inFlight = useRef(false);
  const market = useMarketHours();

  const fetchPortfolio = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
    setError(null);
    try {
      const body = await _fetchPortfolio();
      const stamp = body.fetched_at ?? new Date().toISOString();
      setData(body);
      setLastFetchedAt(stamp);
      // Stash for the next mount of any consumer of this hook.
      _cachedSnapshot = { data: body, lastFetchedAt: stamp };
    } catch (e: any) {
      setError(String(e?.message ?? e));
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
    };
  }, [fetchPortfolio, market]);

  const refreshAll = useCallback(async () => {
    setLoading(true);
    try {
      await _postRefresh({});
      await fetchPortfolio();
    } catch (e: any) {
      setError(String(e?.message ?? e));
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
      } catch (e: any) {
        setError(String(e?.message ?? e));
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
      } catch (e: any) {
        setError(String(e?.message ?? e));
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
