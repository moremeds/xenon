"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
  const q = broker === "FUTU" ? "?broker=FUTU" : "";

  // Track the latest requested broker so a stale in-flight response — from the
  // previous broker, or a slow POST sync that resolves after the user switched
  // account tabs — can't overwrite the current broker's orders. Assigned on
  // every render so it always reflects the broker the user is looking at now.
  const brokerRef = useRef(broker);
  brokerRef.current = broker;

  const triggerSync = useCallback(async () => {
    const reqBroker = broker;
    setSyncing(true);
    try {
      const res = await fetch(`/api/orders${q}`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { error?: string }).error ?? "Sync failed");
      }
      const json = (await res.json()) as OrdersData;
      if (brokerRef.current !== reqBroker) return; // switched tabs mid-flight
      setData(json);
      setLastSync(json.last_sync || null);
      setError(null);
    } catch (err) {
      if (brokerRef.current !== reqBroker) return; // don't surface a stale broker's error
      setError(err instanceof Error ? err.message : "Sync failed");
    } finally {
      setSyncing(false);
    }
  }, [q, broker]);

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
    const reqBroker = broker;
    const init = async () => {
      try {
        const res = await fetch(`/api/orders${q}`);
        if (!res.ok) throw new Error("Failed to fetch orders");
        const json = (await res.json()) as OrdersData;
        if (brokerRef.current !== reqBroker) return; // switched tabs mid-flight
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
        if (brokerRef.current !== reqBroker) return; // stale switch — ignore
        setError(err instanceof Error ? err.message : "Unknown error");
        setLoading(false);
      }
    };

    void init();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [broker]);

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
