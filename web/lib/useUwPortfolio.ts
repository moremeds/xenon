"use client";

/**
 * useUwPortfolio — polling hook for the UW Analyze portfolio dashboard.
 *
 * - Fetches /api/uw-analyze/portfolio as an SSE stream so tickers render
 *   incrementally (cached entries appear near-instantly, fresh scans trickle in).
 * - Falls back to JSON fetch when streaming is unavailable.
 * - Polls every 2 min (open) / 5 min (closed).
 * - Pauses polling when document is hidden (visibilitychange).
 * - Exposes refreshAll(), refreshOne(ticker), addAdhoc(ticker).
 *
 * Backend contract: scripts/api/routes/uw_analyze.py /portfolio + /refresh.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  UwPortfolioResponse,
  UwTickerRow,
  UwActionItem,
} from "@/lib/uwAnalyzeTypes";
import { useMarketHours, MarketState } from "@/lib/useMarketHours";

const POLL_OPEN_MS = 2 * 60 * 1000;
// Note: no POLL_CLOSED_MS — auto-polling is disabled entirely outside
// market hours. See silly-humming-tide.md plan §4. Manual refresh button
// is the only escape hatch during closed/extended hours.

// Module-level cache: survives unmount/remount of components using this
// hook within the same browser session.
type CacheSnapshot = {
  data: UwPortfolioResponse;
  lastFetchedAt: string | null;
};
let _cachedSnapshot: CacheSnapshot | null = null;

/** Test-only: clear the cross-mount snapshot cache. */
export function __resetUwPortfolioCacheForTests(): void {
  _cachedSnapshot = null;
}

export type UseUwPortfolioState = {
  data: UwPortfolioResponse | null;
  loading: boolean;
  error: string | null;
  lastFetchedAt: string | null;
  refreshAll: () => Promise<void>;
  refreshOne: (ticker: string) => Promise<void>;
  addAdhoc: (ticker: string) => Promise<void>;
};

// ---------------------------------------------------------------------------
// SSE streaming fetch
// ---------------------------------------------------------------------------

type SseCallbacks = {
  onMeta: (meta: {
    fetched_at: string;
    market_state: "open" | "closed";
    ttl_seconds: number;
  }) => void;
  onTicker: (row: UwTickerRow) => void;
  onDone: (summary: { action_items: UwActionItem[] }) => void;
};

async function _fetchPortfolioStreaming(
  callbacks: SseCallbacks,
  signal?: AbortSignal,
  opts: { userInitiated?: boolean } = {},
): Promise<void> {
  // `user_initiated=1` tells the backend to bypass the closed-market gate
  // (both the cache gate and the OI-fetch gate) for this request. Set on
  // the follow-up SSE GET after `POST /uw-analyze/refresh` so a manual
  // refresh during closed hours also re-fetches OI baselines. See
  // silly-humming-tide.md plan §2a.
  const url = opts.userInitiated
    ? "/api/uw-analyze/portfolio?user_initiated=1"
    : "/api/uw-analyze/portfolio";
  const res = await fetch(url, {
    cache: "no-store",
    headers: { Accept: "text/event-stream" },
    signal,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`portfolio fetch ${res.status}: ${body.slice(0, 200)}`);
  }
  if (!res.body) throw new Error("no response body for SSE stream");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Parse complete SSE events (delimited by blank lines).
      const events = buffer.split(/\n\n|\r\n\r\n/);
      buffer = events.pop()!; // incomplete last chunk stays in buffer

      for (const event of events) {
        if (!event.trim()) continue;
        const lines = event.split(/\r?\n/);
        let eventType = "message";
        let data = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) eventType = line.slice(7).trim();
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (!data) continue;
        try {
          const parsed = JSON.parse(data);
          if (eventType === "meta") callbacks.onMeta(parsed);
          else if (eventType === "done") callbacks.onDone(parsed);
          else callbacks.onTicker(parsed);
        } catch {
          // Malformed JSON in an SSE event — skip it.
        }
      }
    }
    // Flush decoder for any trailing bytes.
    buffer += decoder.decode();
  } finally {
    reader.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// JSON fetch (fallback, used by refreshAll/refreshOne after POST)
// ---------------------------------------------------------------------------

async function _fetchPortfolioJson(): Promise<UwPortfolioResponse> {
  const res = await fetch("/api/uw-analyze/portfolio", { cache: "no-store" });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`portfolio fetch ${res.status}: ${body.slice(0, 200)}`);
  }
  return res.json();
}

/** Instant cache-only fetch — returns whatever is in memory without running analysis. */
async function _fetchPortfolioCached(): Promise<UwPortfolioResponse> {
  const res = await fetch("/api/uw-analyze/portfolio?cached=true", {
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`cached fetch ${res.status}: ${body.slice(0, 200)}`);
  }
  return res.json();
}

async function _postRefresh(body: {
  tickers?: string[];
  adhoc?: boolean;
}): Promise<void> {
  const res = await fetch("/api/uw-analyze/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`refresh ${res.status}: ${txt.slice(0, 200)}`);
  }
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useUwPortfolio(): UseUwPortfolioState {
  const [data, setData] = useState<UwPortfolioResponse | null>(
    _cachedSnapshot?.data ?? null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastFetchedAt, setLastFetchedAt] = useState<string | null>(
    _cachedSnapshot?.lastFetchedAt ?? null,
  );

  const inFlight = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const market = useMarketHours();

  const fetchPortfolio = useCallback(
    async (opts: { userInitiated?: boolean } = {}) => {
      if (inFlight.current) return;
      inFlight.current = true;

      // Cancel any previous in-flight stream.
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      setLoading(true);
      setError(null);

      try {
        let meta: Partial<UwPortfolioResponse> = {};
        const tickers: UwTickerRow[] = [];
        let actionItems: UwActionItem[] = [];
        // Track the freshest `snapshot_ts` across all streamed rows so
        // `lastFetchedAt` reflects actual data age — NOT the SSE meta
        // `fetched_at` which is response-generation time (backend emits
        // meta before any row is ready). Fix #7 (silly-humming-tide.md).
        let latestSnapshotTs: string | null = null;

        await _fetchPortfolioStreaming(
          {
            onMeta: (m) => {
              meta = m;
            },
            onTicker: (row) => {
              tickers.push(row);
              const ts = row.snapshot_ts;
              if (
                typeof ts === "string" &&
                (latestSnapshotTs === null || ts > latestSnapshotTs)
              ) {
                latestSnapshotTs = ts;
              }
              // Incremental state update — each ticker paints immediately.
              setData((prev) => ({
                ...(prev ?? {
                  fetched_at: "",
                  market_state: "closed" as const,
                  ttl_seconds: 300,
                  tickers: [],
                  action_items: [],
                }),
                ...meta,
                tickers: [...tickers],
              }));
            },
            onDone: (summary) => {
              actionItems = summary.action_items;
              setData((prev) =>
                prev ? { ...prev, action_items: summary.action_items } : prev,
              );
            },
          },
          controller.signal,
          { userInitiated: opts.userInitiated },
        );

        // Prefer the max snapshot_ts across rows; fall back to meta's
        // response_generated_at and finally to client wall clock.
        const stamp =
          latestSnapshotTs ??
          (meta as { response_generated_at?: string }).response_generated_at ??
          new Date().toISOString();
        setLastFetchedAt(stamp);
        _cachedSnapshot = {
          data: {
            ...meta,
            tickers,
            action_items: actionItems,
            // Keep the top-level `fetched_at` consistent with the hook's
            // `lastFetchedAt` so downstream consumers see one truth.
            fetched_at: stamp,
          } as UwPortfolioResponse,
          lastFetchedAt: stamp,
        };
      } catch (e: unknown) {
        // AbortError from superseding fetch — not a real error.
        if (e instanceof Error && e.name === "AbortError") return;
        setError(String(e instanceof Error ? e.message : e));
      } finally {
        setLoading(false);
        inFlight.current = false;
      }
    },
    [],
  );

  // Initial fast cache load → then SSE polling (only when market OPEN).
  // The cached fetch returns whatever is in memory without running analysis,
  // so tiles paint immediately instead of showing blank scaffolds.
  //
  // Closed-market / extended-hours behavior:
  // - Cache prefetch still runs (no UW calls — backend serves from disk).
  // - SSE stream and setInterval are NOT started. The refresh button
  //   (`refreshAll` / `refreshOne`) bypasses the backend gate via
  //   POST /uw-analyze/refresh and is the only way to fetch fresh data.
  // - When market transitions back to OPEN, this effect re-runs (market is
  //   in the dep array), cleanup fires, and polling resumes automatically.
  useEffect(() => {
    let cancelled = false;
    const isAutoPollEnabled = market === MarketState.OPEN;

    const init = async () => {
      // Always run the cache-only prefetch when auto-poll is paused, even
      // if _cachedSnapshot exists — a remount during a long closed-market
      // session should pick up any newer entries the backend has written
      // (e.g., from a recent button click). Plan §4 / Codex issue M9.
      const shouldPrefetch = !_cachedSnapshot || !isAutoPollEnabled;
      if (shouldPrefetch) {
        try {
          const cached = await _fetchPortfolioCached();
          if (!cancelled) {
            // Apply ANY successful response — including `tickers: []` —
            // so a remount during a long closed-market session always
            // reflects the latest disk cache, even when the backend has
            // no entries yet. Fix #6 (silly-humming-tide.md review).
            setData(cached);
            setLastFetchedAt(cached.fetched_at ?? null);
            _cachedSnapshot = {
              data: cached,
              lastFetchedAt: cached.fetched_at ?? null,
            };
          }
        } catch (e: unknown) {
          // When auto-poll is disabled there is no SSE fallback, so a
          // failed prefetch leaves the UI blank/stale with no signal.
          // Surface the error so the header can reflect it. Fix #9
          // (silly-humming-tide.md review). When auto-poll IS enabled,
          // SSE will still run below and can recover, so we stay silent.
          if (!isAutoPollEnabled && !cancelled) {
            setError(String(e instanceof Error ? e.message : e));
          }
        }
      }
      // Skip automatic SSE + polling entirely when market !== OPEN. The
      // backend gate would reject these anyway; skipping saves HTTP round
      // trips. Manual refresh button still works through POST /refresh.
      if (!isAutoPollEnabled) return;
      if (!cancelled) fetchPortfolio();
    };
    void init();

    if (!isAutoPollEnabled) {
      // Still return a cleanup so any in-flight request from a previous
      // OPEN-state mount is cancelled when we transition to non-OPEN.
      return () => {
        cancelled = true;
        abortRef.current?.abort();
      };
    }

    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        fetchPortfolio();
      }
    }, POLL_OPEN_MS);
    const onVis = () => {
      if (document.visibilityState === "visible") fetchPortfolio();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
      abortRef.current?.abort();
    };
  }, [fetchPortfolio, market]);

  // All three mutating actions below follow the same pattern:
  //   1. Abort any in-flight prefetch/SSE (prevents a late cached response
  //      from overwriting the fresher refresh result — plan review fix #5).
  //   2. POST /uw-analyze/refresh — explicit user action, bypasses the
  //      backend closed-market gate via user_initiated=True on that handler.
  //   3. GET /uw-analyze/portfolio?user_initiated=1 (SSE) — propagates the
  //      user-initiated flag into _process_ticker so the OI-fetch path also
  //      bypasses the closed-market gate (plan review fix #1).

  const refreshAll = useCallback(async () => {
    setLoading(true);
    try {
      abortRef.current?.abort();
      await _postRefresh({});
      await fetchPortfolio({ userInitiated: true });
    } catch (e: unknown) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, [fetchPortfolio]);

  const refreshOne = useCallback(
    async (ticker: string) => {
      const t = (ticker ?? "").trim().toUpperCase();
      if (!t) return;
      try {
        abortRef.current?.abort();
        await _postRefresh({ tickers: [t] });
        await fetchPortfolio({ userInitiated: true });
      } catch (e: unknown) {
        setError(String(e instanceof Error ? e.message : e));
      }
    },
    [fetchPortfolio],
  );

  const addAdhoc = useCallback(
    async (ticker: string) => {
      const t = (ticker ?? "").trim().toUpperCase();
      if (!t) return;
      try {
        abortRef.current?.abort();
        await _postRefresh({ tickers: [t], adhoc: true });
        await fetchPortfolio({ userInitiated: true });
      } catch (e: unknown) {
        setError(String(e instanceof Error ? e.message : e));
      }
    },
    [fetchPortfolio],
  );

  return {
    data,
    loading,
    error,
    lastFetchedAt,
    refreshAll,
    refreshOne,
    addAdhoc,
  };
}
