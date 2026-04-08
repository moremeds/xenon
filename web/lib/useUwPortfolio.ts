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
  const [data, setData] = useState<UwPortfolioResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastFetchedAt, setLastFetchedAt] = useState<string | null>(null);

  const inFlight = useRef(false);
  const market = useMarketHours();

  const fetchPortfolio = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
    setError(null);
    try {
      const body = await _fetchPortfolio();
      setData(body);
      setLastFetchedAt(body.fetched_at ?? new Date().toISOString());
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
