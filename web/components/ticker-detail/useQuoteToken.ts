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
  /** Cached tokens from modal open; used to gate the UI submit button.
   *  DO NOT submit these — the server enforces a 500ms TTL, so tokens
   *  older than half a second will be rejected as STALE_QUOTE. Call
   *  mintNow() at submit time for a fresh bag instead. */
  tokens: Record<string, string> | null;
  error: string | null;
  /** Force a re-mint of all tokens; use when a transient fetch failure
   *  disables submit and the user wants to retry without reopening the
   *  modal. Resets tokens/error state before retrying. */
  reload: () => void;
  /** Fetch a fresh token bag synchronously right before POST. Bypasses
   *  the state cache — returns the freshly-minted tokens. The server's
   *  500ms token TTL means cached tokens from modal open will reliably
   *  reject on any user interaction latency; always submit with
   *  mintNow() rather than `tokens`. Returns null on leg/error
   *  preconditions. */
  mintNow: () => Promise<Record<string, string> | null>;
};

async function mintLegs(legs: Leg[]): Promise<Record<string, string> | null> {
  if (legs.length === 0) return null;
  if (legs.some((l) => l.conId == null)) return null;
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
  return Object.fromEntries(results);
}

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
        const bag = await mintLegs(legs);
        if (!cancelled && bag) setTokens(bag);
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
  const mintNow = useCallback(() => mintLegs(legs), [legsKey]); // eslint-disable-line react-hooks/exhaustive-deps

  return { tokens, error, reload, mintNow };
}
