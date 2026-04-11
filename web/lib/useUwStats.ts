"use client";

import { useEffect, useState } from "react";

export type UwStatsSnapshot = {
  totals: {
    requests: number;
    success: number;
    cached: number;
    retries: number;
    failures: number;
    rate_limits: number;
    connection_errors: number;
  };
  latency_ms: {
    samples: number;
    min?: number;
    max?: number;
    avg?: number;
    p95?: number;
  };
  // Raw HTTP status → count map from the backend collector.
  // Keys arrive as numeric strings in JSON (e.g. "200"), so typed as
  // string-indexed here and narrowed by the consumer.
  by_status?: Record<string, number>;
  uptime_seconds: number;
};

const POLL_INTERVAL_MS = 10_000;

export function useUwStats(): UwStatsSnapshot | null {
  const [stats, setStats] = useState<UwStatsSnapshot | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchStats = async () => {
      try {
        const res = await fetch("/api/uw-stats", { cache: "no-store" });
        if (!res.ok) return;
        const data = (await res.json()) as UwStatsSnapshot;
        if (!cancelled) setStats(data);
      } catch {
        // Silent fail — sidebar will show "—" placeholders.
      }
    };

    fetchStats();
    const id = setInterval(fetchStats, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return stats;
}
