"use client";
import { useEffect, useState } from "react";

type Options = { ticker: string; conId: number; expiry: string | null };

export function useQuoteToken({ ticker, conId, expiry }: Options) {
  const [token, setToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      try {
        const res = await fetch(
          `/api/orders/quote?ticker=${encodeURIComponent(ticker)}&con_id=${conId}`,
        );
        if (!res.ok) throw new Error(`quote ${res.status}`);
        const j = await res.json();
        if (!cancelled) setToken(j.token);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [ticker, conId, expiry]);

  return { token, error };
}
