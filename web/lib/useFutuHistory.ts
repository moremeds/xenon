"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import type {
  EquityCurvePoint,
  FutuTradesPage,
  FutuPerformance,
  FutuReconciliation,
  FutuSyncResult,
} from "@/lib/futuHistory/types";

const j = async <T>(url: string): Promise<T> => {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json() as Promise<T>;
};

interface FetchState<T> {
  data: T | null;
  isLoading: boolean;
  error: Error | null;
}

function useGet<T>(url: string | null, deps: unknown[]): FetchState<T> {
  const [state, setState] = useState<FetchState<T>>({
    data: null,
    isLoading: !!url,
    error: null,
  });
  useEffect(() => {
    if (!url) return;
    let aborted = false;
    setState((s) => ({ ...s, isLoading: true }));
    j<T>(url)
      .then((data) => {
        if (!aborted) setState({ data, isLoading: false, error: null });
      })
      .catch((error) => {
        if (!aborted)
          setState({ data: null, isLoading: false, error: error as Error });
      });
    return () => {
      aborted = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

export function useFutuEquityCurve(range: { from: string; to: string }) {
  return useGet<EquityCurvePoint[]>(
    `/api/futu/equity-curve?from=${range.from}&to=${range.to}`,
    [range.from, range.to],
  );
}

export interface TradesQuery {
  from: string;
  to: string;
  ticker?: string;
  action?: "BUY" | "SELL";
  sort?: "filled_at" | "realized_pnl" | "ticker" | "fees";
  direction?: "asc" | "desc";
  limit?: number;
  cursor?: string;
}

export function useFutuTrades(q: TradesQuery) {
  const params = new URLSearchParams(
    Object.entries(q)
      .filter(([, v]) => v !== undefined)
      .map(([k, v]) => [k, String(v)]) as [string, string][],
  );
  return useGet<FutuTradesPage>(`/api/futu/trades?${params}`, [
    q.from,
    q.to,
    q.ticker,
    q.action,
    q.sort,
    q.direction,
    q.limit,
    q.cursor,
  ]);
}

export function useFutuPerformance(range: { from: string; to: string }) {
  return useGet<FutuPerformance>(
    `/api/futu/performance?from=${range.from}&to=${range.to}`,
    [range.from, range.to],
  );
}

export function useFutuReconciliation() {
  const state = useGet<FutuReconciliation>("/api/futu/reconciliation", []);
  // Lightweight 5-min poll to match the spirit of the original
  // refetchInterval, without pulling in @tanstack/react-query.
  useEffect(() => {
    const id = setInterval(() => {
      j<FutuReconciliation>("/api/futu/reconciliation").catch(() => {});
    }, 5 * 60_000);
    return () => clearInterval(id);
  }, []);
  return state;
}

interface UseFutuSyncReturn {
  mutate: (body: { since?: string; rematch?: boolean }) => void;
  mutateAsync: (body: {
    since?: string;
    rematch?: boolean;
  }) => Promise<FutuSyncResult>;
  isPending: boolean;
  data: FutuSyncResult | null;
  error: Error | null;
}

export function useFutuSync(): UseFutuSyncReturn {
  const [data, setData] = useState<FutuSyncResult | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [isPending, setIsPending] = useState(false);
  const inflight = useRef(false);

  const mutateAsync = useCallback(
    async (body: { since?: string; rematch?: boolean }) => {
      if (inflight.current) {
        throw new Error("sync already in flight");
      }
      inflight.current = true;
      setIsPending(true);
      setError(null);
      try {
        const r = await fetch("/api/futu/history/sync", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            since: body.since ?? "auto",
            rematch: body.rematch ?? false,
          }),
        });
        if (!r.ok) throw new Error(`sync failed: ${r.status}`);
        const payload: FutuSyncResult = await r.json();
        setData(payload);
        return payload;
      } catch (err) {
        setError(err as Error);
        throw err;
      } finally {
        inflight.current = false;
        setIsPending(false);
      }
    },
    [],
  );

  const mutate = useCallback(
    (body: { since?: string; rematch?: boolean }) => {
      void mutateAsync(body).catch(() => {});
    },
    [mutateAsync],
  );

  return { mutate, mutateAsync, isPending, data, error };
}
