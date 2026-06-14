"use client";

import { useEffect, useState } from "react";
import {
  parseRealtimeSubscribers,
  type RealtimeSubscribers,
} from "./subscriberHealth";

const EMPTY: RealtimeSubscribers = {
  reachable: false,
  subscribers: [],
  anonymousCount: 0,
};

export function useSubscriberHealth(intervalMs = 10_000): RealtimeSubscribers {
  const [state, setState] = useState<RealtimeSubscribers>(EMPTY);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setState(parseRealtimeSubscribers(data));
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
