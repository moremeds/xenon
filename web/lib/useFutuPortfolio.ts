"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PortfolioData } from "@/lib/types";
import {
  futuToPortfolioData,
  isFutuNeverSynced,
  type FutuPortfolioEnvelope,
  type FutuPortfolioResponse,
} from "@/lib/futuPortfolioAdapter";

/**
 * Hook: fetch the Futu portfolio from `/api/futu/portfolio` and adapt
 * it to the same `PortfolioData` shape IB uses, so downstream components
 * (MetricCards, WorkspaceSections) consume it without branching on broker.
 *
 * Design:
 * - Initial load: GET the cached JSON (fast, always succeeds if OpenD has
 *   ever been synced).
 * - First boot: GET returns HTTP 200 with `{ok:false, code:"never_synced"}`
 *   instead of 404 — the hook type-guards and exposes `neverSynced=true`
 *   so the UI can render a "click sync" prompt distinctly from an error.
 * - `syncNow()`: POSTs to force a live fetch. Used by the header sync
 *   button when the Futu tab is active.
 * - Polls at 30s intervals when `enabled` is true (matches market-hours
 *   behavior of usePortfolio).
 * - When `enabled` flips false, polling stops but last data stays cached.
 */
export function useFutuPortfolio(enabled: boolean) {
  const [data, setData] = useState<PortfolioData | null>(null);
  const [envelope, setEnvelope] = useState<FutuPortfolioEnvelope | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastSync, setLastSync] = useState<string | null>(null);
  const [neverSynced, setNeverSynced] = useState(false);

  const inFlightRef = useRef(false);

  const applyResponse = useCallback((body: FutuPortfolioResponse) => {
    if (isFutuNeverSynced(body)) {
      setEnvelope(null);
      setData(null);
      setLastSync(null);
      setNeverSynced(true);
      setError(null);
      return;
    }
    // body is narrowed to FutuPortfolioEnvelope
    setEnvelope(body);
    setData(futuToPortfolioData(body));
    setLastSync(body.fetched_at);
    setNeverSynced(false);
    setError(null);
  }, []);

  const loadFromCache = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
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
      inFlightRef.current = false;
    }
  }, [applyResponse]);

  const syncNow = useCallback(async () => {
    if (syncing) return;
    setSyncing(true);
    setError(null);
    try {
      const res = await fetch("/api/futu/portfolio", {
        method: "POST",
        cache: "no-store",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `POST /api/futu/portfolio: ${res.status}`);
      }
      const body: FutuPortfolioResponse = await res.json();
      applyResponse(body);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSyncing(false);
    }
  }, [syncing, applyResponse]);

  // Initial load + polling while enabled.
  useEffect(() => {
    void loadFromCache();
    if (!enabled) return;
    const id = setInterval(() => {
      void loadFromCache();
    }, 30_000);
    return () => clearInterval(id);
  }, [enabled, loadFromCache]);

  return { data, envelope, syncing, error, lastSync, syncNow, neverSynced };
}
