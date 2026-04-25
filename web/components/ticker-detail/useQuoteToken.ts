"use client";
import { useEffect, useState } from "react";

type Options = { ticker: string; conId: number | null; expiry: string | null };

export function useQuoteToken({ ticker, conId, expiry }: Options) {
  const [token, setToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      if (conId == null || conId <= 0) {
        setToken(null);
        setError(null);
        return;
      }
      try {
        setError(null);
        const res = await fetch(
          `/api/orders/quote?ticker=${encodeURIComponent(ticker)}&con_id=${conId}`,
        );
        if (!res.ok) {
          const body = await res.json().catch(() => null);
          const detail =
            body && typeof body.detail === "string"
              ? body.detail
              : `quote ${res.status}`;
          throw new Error(detail);
        }
        const j = await res.json();
        if (!cancelled) setToken(j.token);
      } catch (e) {
        if (!cancelled) {
          setToken(null);
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [ticker, conId, expiry]);

  return { token, error };
}
