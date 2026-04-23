"use client";
import { useCallback, useEffect, useState } from "react";

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

type Leg = { ticker: string; conId: number | null; expiry: string | null };

type TokensResult = {
  tokens: Record<string, string> | null;
  error: string | null;
  /** Force a re-mint of all tokens; use when a transient fetch failure
   *  disables submit and the user wants to retry without reopening the
   *  modal. Resets tokens/error state before retrying. */
  reload: () => void;
};

export function useQuoteTokens({ legs }: { legs: Leg[] }): TokensResult {
  const [tokens, setTokens] = useState<Record<string, string> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const legsKey = JSON.stringify(
    legs.map((l) => [l.ticker, l.conId, l.expiry]),
  );

  useEffect(() => {
    let cancelled = false;
    async function run() {
      setTokens(null);
      setError(null);
      if (legs.length === 0) return;
      if (legs.some((l) => l.conId == null)) {
        setError("missing conId on one or more legs");
        return;
      }
      try {
        const results = await Promise.all(
          legs.map(async (l) => {
            const res = await fetch(
              `/api/orders/quote?ticker=${encodeURIComponent(l.ticker)}&con_id=${l.conId}`,
            );
            if (!res.ok) throw new Error(`quote ${res.status} for ${l.conId}`);
            const j = await res.json();
            return [String(l.conId), j.token as string] as const;
          }),
        );
        if (!cancelled) {
          setTokens(Object.fromEntries(results));
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [legsKey, reloadNonce]); // eslint-disable-line react-hooks/exhaustive-deps

  const reload = useCallback(() => setReloadNonce((n) => n + 1), []);

  return { tokens, error, reload };
}
