"use client";

import { useCallback, useEffect, useState } from "react";

export type WatchlistEntry = {
  id: string;
  symbol: string;
  sector: string | null;
  added_at: string;
};

type UseWatchlistReturn = {
  watchlist: WatchlistEntry[];
  isLoading: boolean;
  isWatched: (symbol: string) => boolean;
  toggleWatch: (symbol: string, sector?: string) => Promise<void>;
};

// Module-level shared store: one fetch hydrates every consumer.
let cache: WatchlistEntry[] = [];
let loaded = false;
let inFlight: Promise<void> | null = null;
const subscribers = new Set<() => void>();

function notify(): void {
  for (const fn of subscribers) fn();
}

function setCache(next: WatchlistEntry[]): void {
  cache = next;
  notify();
}

async function loadWatchlist(): Promise<void> {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    try {
      const res = await fetch("/api/watchlist", { cache: "no-store" });
      if (!res.ok) throw new Error("Failed to fetch watchlist");
      const json = (await res.json()) as { watchlist: WatchlistEntry[] };
      cache = Array.isArray(json.watchlist) ? json.watchlist : [];
    } catch {
      // keep whatever we had
    } finally {
      loaded = true;
      inFlight = null;
      notify();
    }
  })();
  return inFlight;
}

export function useWatchlist(): UseWatchlistReturn {
  const [, forceRender] = useState(0);
  const [isLoading, setIsLoading] = useState(!loaded);

  useEffect(() => {
    const rerender = () => {
      forceRender((n) => n + 1);
      setIsLoading(!loaded);
    };
    subscribers.add(rerender);
    if (!loaded && !inFlight) void loadWatchlist();
    else setIsLoading(!loaded);
    return () => {
      subscribers.delete(rerender);
    };
  }, []);

  const isWatched = useCallback((symbol: string) => {
    const normalized = symbol.trim().toUpperCase();
    return cache.some((w) => w.symbol === normalized);
  }, []);

  const toggleWatch = useCallback(async (symbol: string, sector?: string) => {
    const normalized = symbol.trim().toUpperCase();
    const previous = cache;
    const already = cache.some((w) => w.symbol === normalized);

    if (already) {
      setCache(cache.filter((w) => w.symbol !== normalized));
      try {
        const res = await fetch(`/api/watchlist/${encodeURIComponent(normalized)}`, {
          method: "DELETE",
          cache: "no-store",
        });
        if (!res.ok) throw new Error("Failed to remove from watchlist");
      } catch (err) {
        setCache(previous);
        throw err;
      }
      return;
    }

    const optimistic: WatchlistEntry = {
      id: `optimistic-${normalized}`,
      symbol: normalized,
      sector: sector ?? null,
      added_at: new Date().toISOString(),
    };
    setCache([optimistic, ...cache]);
    try {
      const res = await fetch("/api/watchlist", {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: normalized, sector }),
      });
      if (!res.ok) throw new Error("Failed to add to watchlist");
      await loadWatchlist();
    } catch (err) {
      setCache(previous);
      throw err;
    }
  }, []);

  return { watchlist: cache, isLoading, isWatched, toggleWatch };
}
