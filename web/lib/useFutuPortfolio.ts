"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PortfolioData } from "@/lib/types";
import {
  futuToPortfolioData,
  isFutuNeverSynced,
  type FutuPortfolioEnvelope,
  type FutuPortfolioResponse,
} from "@/lib/futuPortfolioAdapter";

const BASE_INTERVAL_MS = 30_000;
const MAX_INTERVAL_MS = 300_000; // 5 min cap on backoff

/**
 * Hook: fetch the Futu portfolio from `/api/futu/portfolio` and adapt
 * it to the same `PortfolioData` shape IB uses, so downstream components
 * (MetricCards, WorkspaceSections) consume it without branching on broker.
 *
 * Design:
 * - Initial load: GET the cached JSON (fast, always succeeds if OpenD has
 *   ever been synced).
 * - Polling: POST every 30s to trigger a live sync from OpenD (matches
 *   the IB hook's pattern). Exponential backoff on error (30s → 5min).
 * - `syncNow()`: manual trigger that resets backoff and rearms the loop.
 * - Tab visibility: resets backoff and forces a sync when user returns.
 */
export function useFutuPortfolio(enabled: boolean) {
  const [data, setData] = useState<PortfolioData | null>(null);
  const [envelope, setEnvelope] = useState<FutuPortfolioEnvelope | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastSync, setLastSync] = useState<string | null>(null);
  const [neverSynced, setNeverSynced] = useState(false);

  const syncingRef = useRef(false);
  const intervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoffRef = useRef(BASE_INTERVAL_MS);
  const didInitialReadRef = useRef(false);
  const initialLoadStartedRef = useRef(false);
  const syncLoopArmedRef = useRef(false);
  const doSyncRef = useRef<() => Promise<void>>(async () => {});

  const applyResponse = useCallback((body: FutuPortfolioResponse) => {
    if (isFutuNeverSynced(body)) {
      setEnvelope(null);
      setData(null);
      setLastSync(null);
      setNeverSynced(true);
      setError(null);
      return;
    }
    setEnvelope(body);
    setData(futuToPortfolioData(body));
    setLastSync(body.fetched_at);
    setNeverSynced(false);
    setError(null);
  }, []);

  const loadFromCache = useCallback(async () => {
    try {
      const res = await fetch("/api/futu/portfolio", { cache: "no-store" });
      if (!res.ok) {
        throw new Error(`GET /api/futu/portfolio: ${res.status}`);
      }
      const body: FutuPortfolioResponse = await res.json();
      applyResponse(body);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      didInitialReadRef.current = true;
    }
  }, [applyResponse]);

  const scheduleNext = useCallback(
    (delay: number) => {
      if (!enabled) return;
      if (intervalRef.current) clearTimeout(intervalRef.current);
      intervalRef.current = setTimeout(() => {
        void doSyncRef.current();
      }, delay);
    },
    [enabled],
  );

  const doSync = useCallback(async () => {
    if (syncingRef.current) return;
    syncingRef.current = true;
    setSyncing(true);
    try {
      const res = await fetch("/api/futu/portfolio", {
        method: "POST",
        cache: "no-store",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(
          (body as { error?: string }).error ||
            `POST /api/futu/portfolio: ${res.status}`,
        );
      }
      const body: FutuPortfolioResponse = await res.json();
      applyResponse(body);
      backoffRef.current = BASE_INTERVAL_MS;
      scheduleNext(BASE_INTERVAL_MS);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      backoffRef.current = Math.min(backoffRef.current * 2, MAX_INTERVAL_MS);
      scheduleNext(backoffRef.current);
    } finally {
      syncingRef.current = false;
      setSyncing(false);
    }
  }, [applyResponse, scheduleNext]);

  doSyncRef.current = doSync;

  const syncNow = useCallback(() => {
    backoffRef.current = BASE_INTERVAL_MS;
    syncLoopArmedRef.current = true;
    void doSync();
  }, [doSync]);

  // Initial load: fast GET from cache, then arm the POST sync loop.
  useEffect(() => {
    if (initialLoadStartedRef.current) return;
    initialLoadStartedRef.current = true;

    let cancelled = false;

    const init = async () => {
      await loadFromCache();
      if (cancelled) return;
      if (enabled) {
        syncLoopArmedRef.current = true;
        scheduleNext(BASE_INTERVAL_MS);
      }
    };

    void init();

    return () => {
      cancelled = true;
      if (intervalRef.current) {
        clearTimeout(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [enabled, loadFromCache, scheduleNext]);

  // Start syncing when `enabled` transitions to true after mount.
  useEffect(() => {
    if (!enabled) {
      if (intervalRef.current) {
        clearTimeout(intervalRef.current);
        intervalRef.current = null;
      }
      syncLoopArmedRef.current = false;
      return;
    }

    if (!didInitialReadRef.current || syncLoopArmedRef.current) return;
    syncLoopArmedRef.current = true;
    void doSync();
  }, [enabled, doSync]);

  // Reset backoff & force sync when tab becomes visible again.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible" && enabled) {
        backoffRef.current = BASE_INTERVAL_MS;
        if (!syncingRef.current) {
          syncLoopArmedRef.current = true;
          scheduleNext(500);
        }
      }
    };
    if (enabled) {
      document.addEventListener("visibilitychange", onVisible);
      return () => document.removeEventListener("visibilitychange", onVisible);
    }
  }, [scheduleNext, enabled]);

  return { data, envelope, syncing, error, lastSync, syncNow, neverSynced };
}
