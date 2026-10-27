"use client";

import { useEffect, useState } from "react";
import {
  parseRealtimeSubscribers,
  type RealtimeSubscribers,
} from "./subscriberHealth";
import {
  parseIbUpstream,
  UNKNOWN_UPSTREAM,
  type IbUpstream,
} from "./ibUpstream";

/** The /api/health poll also carries the IB upstream state (one poll, two readings). */
export type HealthReadings = RealtimeSubscribers & { ibUpstream: IbUpstream };

const EMPTY: HealthReadings = {
  reachable: false,
  subscribers: [],
  anonymousCount: 0,
  ibUpstream: UNKNOWN_UPSTREAM,
};

export function useSubscriberHealth(intervalMs = 10_000): HealthReadings {
  const [state, setState] = useState<HealthReadings>(EMPTY);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled)
          setState({
            ...parseRealtimeSubscribers(data),
            ibUpstream: parseIbUpstream(data),
          });
      } catch {
        // Silent: backend may be down during frontend-only dev sessions.
      }
    }
    poll();
    const timer = setInterval(poll, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [intervalMs]);

  return state;
}
