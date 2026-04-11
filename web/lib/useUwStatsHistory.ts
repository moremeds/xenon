"use client";

import { useEffect, useState } from "react";

export type UwHourlyBucket = {
  hour: string; // ISO8601 with Z suffix, e.g. "2026-04-10T14:00:00Z"
  requests_2xx: number;
  requests_4xx: number;
  requests_5xx: number;
  cached: number;
  avg_latency_ms: number | null;
};

export type UwStatsHistory = {
  buckets: UwHourlyBucket[];
};

// History moves slower than session counters — 60s is plenty and
// avoids pointless re-renders of a 96-bar chart.
const POLL_INTERVAL_MS = 60_000;

export function useUwStatsHistory(hours: number = 96): UwStatsHistory | null {
  const [history, setHistory] = useState<UwStatsHistory | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchHistory = async () => {
      try {
        const res = await fetch(`/api/uw-stats/history?hours=${hours}`, {
          cache: "no-store",
        });
        if (!res.ok) return;
        const data = (await res.json()) as UwStatsHistory;
        if (!cancelled) setHistory(data);
      } catch {
        // Silent fail — chart will render zero-filled when data is null.
      }
    };

    fetchHistory();
    const id = setInterval(fetchHistory, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [hours]);

  return history;
}
