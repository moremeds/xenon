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
const STALE_CACHE_MS = 60_000; // cached snapshot older than this: sync right away
const STALE_FIRST_SYNC_DELAY_MS = 500;

/** True when a cached snapshot is old enough that waiting a full poll interval would show stale data. */
function isStaleSnapshot(fetchedAt: string | null | undefined): boolean {
  if (!fetchedAt) return false;
  const ts = Date.parse(fetchedAt);
  return !Number.isFinite(ts) || Date.now() - ts > STALE_CACHE_MS;
}

/**
 * Result of the initial GET /api/futu/portfolio read (and any later
 * applies). Lets consumers distinguish "still loading" from definitive
 * empty states instead of rendering LOADING forever:
 * - `pending`      — mount read hasn't completed yet
 * - `loaded`       — a real snapshot arrived
 * - `never_synced` — FastAPI has no snapshot (404, or the never_synced
 *                    200 envelope): an account condition, not an error
 * - `unavailable`  — FastAPI/Next failed to return a snapshot (502/other)
 */
export type FutuInitialRead =
  | "pending"
  | "loaded"
  | "never_synced"
  | "unavailable";

/**
 * Hook: fetch the Futu portfolio from `/api/futu/portfolio` and adapt
 * it to the same `PortfolioData` shape IB uses, so downstream components
 * (MetricCards, WorkspaceSections) consume it without branching on broker.
 *
 * Design:
 * - Initial load: GET the cached JSON (fast, always succeeds if OpenD has
 *   ever been synced). When that snapshot is older than 60s the first
 *   POST sync runs after 500ms instead of waiting a full interval.
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
  const [initialRead, setInitialRead] = useState<FutuInitialRead>("pending");
  // True once the mount-time cache GET has settled (any outcome).
  const [initialReadDone, setInitialReadDone] = useState(false);

  const syncingRef = useRef(false);
  const intervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoffRef = useRef(BASE_INTERVAL_MS);
  const initialLoadStartedRef = useRef(false);
  // `fetched_at` of the newest snapshot we hold; drives the first-sync delay.
  const latestFetchedAtRef = useRef<string | null>(null);
  const doSyncRef = useRef<() => Promise<void>>(async () => {});

  const applyResponse = useCallback((body: FutuPortfolioResponse) => {
    latestFetchedAtRef.current = isFutuNeverSynced(body) ? null : body.fetched_at;
    if (isFutuNeverSynced(body)) {
      setEnvelope(null);
      setData(null);
      setLastSync(null);
      setNeverSynced(true);
      setInitialRead("never_synced");
      setError(null);
      return;
    }
    setEnvelope(body);
    setData(futuToPortfolioData(body));
    setLastSync(body.fetched_at);
    setNeverSynced(false);
    setInitialRead("loaded");
    setError(null);
  }, []);

  const loadFromCache = useCallback(async () => {
    try {
      const res = await fetch("/api/futu/portfolio", { cache: "no-store" });
      if (res.status === 404) {
        // FastAPI has no snapshot — never synced is a measurement
        // condition, not an error. Mirror the never_synced body path.
        setEnvelope(null);
        setData(null);
        setLastSync(null);
        setNeverSynced(true);
        setInitialRead("never_synced");
        setError(null);
        return;
      }
      if (!res.ok) {
        throw new Error(`GET /api/futu/portfolio: ${res.status}`);
      }
      const body: FutuPortfolioResponse = await res.json();
      applyResponse(body);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setInitialRead("unavailable");
    } finally {
      setInitialReadDone(true);
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
        headers: { "Content-Type": "application/json" },
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
    void doSync();
  }, [doSync]);

  // Initial load: one fast GET from cache. Deliberately NOT cancelled when
  // `enabled` changes (useMarketHours starts CLOSED and flips after its own
  // effect); the arming effect below decides when syncing starts.
  useEffect(() => {
    if (initialLoadStartedRef.current) return;
    initialLoadStartedRef.current = true;
    void loadFromCache();
  }, [loadFromCache]);

  // Single place that starts the POST loop: needs BOTH the initial read done
  // AND `enabled`, in either order. First sync is quick (500ms) when we hold
  // no snapshot or it is stale, else one normal interval.
  useEffect(() => {
    if (!enabled || !initialReadDone) return;
    const fetchedAt = latestFetchedAtRef.current;
    scheduleNext(
      fetchedAt === null || isStaleSnapshot(fetchedAt)
        ? STALE_FIRST_SYNC_DELAY_MS
        : BASE_INTERVAL_MS,
    );
    return () => {
      if (intervalRef.current) {
        clearTimeout(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [enabled, initialReadDone, scheduleNext]);

  // Reset backoff & force sync when tab becomes visible again.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible" && enabled) {
        backoffRef.current = BASE_INTERVAL_MS;
        if (!syncingRef.current) {
          scheduleNext(500);
        }
      }
    };
    if (enabled) {
      document.addEventListener("visibilitychange", onVisible);
      return () => document.removeEventListener("visibilitychange", onVisible);
    }
  }, [scheduleNext, enabled]);

  return {
    data,
    envelope,
    syncing,
    error,
    lastSync,
    syncNow,
    neverSynced,
    initialRead,
  };
}
