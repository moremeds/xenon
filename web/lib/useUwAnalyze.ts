"use client";

import { useCallback, useRef, useState } from "react";
import type { UwAnalyzeResponse } from "@/lib/types/uwAnalyze";

export interface UseUwAnalyzeState {
  loading: boolean;
  data: UwAnalyzeResponse | null;
  error: string | null;
  lastRunAt: string | null;
  analyse: (ticker: string) => Promise<void>;
  reset: () => void;
}

export function useUwAnalyze(): UseUwAnalyzeState {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<UwAnalyzeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastRunAt, setLastRunAt] = useState<string | null>(null);
  // In-flight dedupe — ignore second call while one is running.
  const inFlight = useRef(false);

  const analyse = useCallback(async (ticker: string) => {
    if (inFlight.current) return;
    const t = (ticker ?? "").trim().toUpperCase();
    if (!t) {
      setError("Enter a ticker");
      return;
    }
    inFlight.current = true;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/uw-analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker: t }),
      });
      if (!res.ok) {
        let msg: string;
        try {
          const body = await res.json();
          msg = body.error ?? `HTTP ${res.status}`;
        } catch {
          msg = `HTTP ${res.status}`;
        }
        setError(msg);
        setData(null);
        return;
      }
      const json = (await res.json()) as UwAnalyzeResponse;
      setData(json);
      setLastRunAt(new Date().toISOString());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
      setData(null);
    } finally {
      setLoading(false);
      inFlight.current = false;
    }
  }, []);

  const reset = useCallback(() => {
    setData(null);
    setError(null);
    setLastRunAt(null);
  }, []);

  return { loading, data, error, lastRunAt, analyse, reset };
}
