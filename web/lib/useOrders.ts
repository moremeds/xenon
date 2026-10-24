"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { futuScopeQuery, useFutuAccountEnv } from "./futuAccountEnv";
import { errorFromResponseBody } from "./order/responseError";
import type { OrdersData } from "./types";

const SYNC_INTERVAL_MS = 30_000;

type UseOrdersReturn = {
  data: OrdersData | null;
  loading: boolean;
  syncing: boolean;
  error: string | null;
  lastSync: string | null;
  syncNow: () => void;
  updateData: (data: OrdersData) => void;
};

export function useOrders(
  active: boolean = true,
  broker: "IB" | "FUTU" = "IB",
): UseOrdersReturn {
  const [data, setData] = useState<OrdersData | null>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastSync, setLastSync] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const didInitialSync = useRef(false);
  const futuEnv = useFutuAccountEnv();
  const q = useMemo(
    () => (broker === "FUTU" ? futuScopeQuery(futuEnv) : ""),
    [broker, futuEnv],
  );

  // Track the latest requested broker + Futu env so a stale in-flight response
  // can't overwrite the current scope after a tab or SIMULATE/REAL switch.
  const scopeRef = useRef(broker === "FUTU" ? `FUTU:${futuEnv}` : "IB");
  scopeRef.current = broker === "FUTU" ? `FUTU:${futuEnv}` : "IB";

  const triggerSync = useCallback(async () => {
    const reqScope = scopeRef.current;
    setSyncing(true);
    try {
      const res = await fetch(`/api/orders${q}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(errorFromResponseBody(body, "Sync failed"));
      }
      const json = (await res.json()) as OrdersData;
      if (scopeRef.current !== reqScope) return;
      setData(json);
      setLastSync(json.last_sync || null);
      setError(null);
    } catch (err) {
      if (scopeRef.current !== reqScope) return;
      setError(err instanceof Error ? err.message : "Sync failed");
    } finally {
      setSyncing(false);
    }
  }, [q]);

  const syncNow = useCallback(() => {
    void triggerSync();
  }, [triggerSync]);

  // Always read cached orders on mount (and on broker switch). Only auto-sync
  // when active (orders page). Re-runs when `broker` changes so switching the
  // account tab repaints the other broker's orders.
  useEffect(() => {
    didInitialSync.current = false;
    setLoading(true);
    // Clear the previous broker's orders immediately so a tab switch never
    // lingers on (or flickers back to) the other account's open orders while
    // the new broker's cached read is in flight.
    setData(null);
    setError(null);
    const reqScope = scopeRef.current;
    const init = async () => {
      try {
        const res = await fetch(`/api/orders${q}`);
        if (!res.ok) throw new Error("Failed to fetch orders");
        const json = (await res.json()) as OrdersData;
        if (scopeRef.current !== reqScope) return;
        setData(json);
        setLastSync(json.last_sync || null);
        setError(null);
        setLoading(false);

        // Sync fresh on first load of orders page for this broker.
        if (active && !didInitialSync.current) {
          didInitialSync.current = true;
          void triggerSync();
        }
      } catch (err) {
        if (scopeRef.current !== reqScope) return;
        setError(err instanceof Error ? err.message : "Unknown error");
        setLoading(false);
      }
    };

    void init();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [broker, futuEnv]);

  // When orders page becomes active, trigger IB sync if we haven't yet
  useEffect(() => {
    if (active && !didInitialSync.current && data != null) {
      didInitialSync.current = true;
      void triggerSync();
    }
  }, [active, data, triggerSync]);

  // Auto-sync interval (only when active)
  useEffect(() => {
    if (!active) {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      return;
    }

    intervalRef.current = setInterval(() => {
      void triggerSync();
    }, SYNC_INTERVAL_MS);

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [active, triggerSync]);

  const updateData = useCallback((newData: OrdersData) => {
    setData(newData);
    setLastSync(newData.last_sync || null);
    setError(null);
  }, []);

  return { data, loading, syncing, error, lastSync, syncNow, updateData };
}
