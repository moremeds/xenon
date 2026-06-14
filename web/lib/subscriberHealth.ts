export type SubscriberHealth = {
  id: string;
  connected: boolean;
  lastPongMsAgo?: number;
  offlineForMs?: number;
  lastSeenMsAgo?: number;
};

export type RealtimeSubscribers = {
  reachable: boolean;
  subscribers: SubscriberHealth[];
  anonymousCount: number;
};

export type SubscriberLiveness = "live" | "stale" | "offline";

const LIVE_MAX_MS = 35_000;

export function classifySubscriber(s: SubscriberHealth): SubscriberLiveness {
  if (!s.connected) return "offline";
  return (s.lastPongMsAgo ?? 0) < LIVE_MAX_MS ? "live" : "stale";
}

export function formatAge(ms: number | undefined): string {
  if (ms == null) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.round(m / 60)}h`;
}

export const DOT_CLASS: Record<SubscriberLiveness, string> = {
  live: "status-dot-live",
  stale: "status-dot-stale",
  offline: "status-dot-dead",
};

type RawRow = {
  id: string;
  connected: boolean;
  last_pong_ms_ago?: number;
  offline_for_ms?: number | null;
  last_seen_ms_ago?: number;
};
type RawBlock = {
  reachable?: boolean;
  subscribers?: RawRow[];
  anonymous_count?: number;
};

export function parseRealtimeSubscribers(
  health: { realtime_subscribers?: RawBlock } | null | undefined,
): RealtimeSubscribers {
  const block = health?.realtime_subscribers;
  if (!block) return { reachable: false, subscribers: [], anonymousCount: 0 };
  return {
    reachable: Boolean(block.reachable),
    anonymousCount: block.anonymous_count ?? 0,
    subscribers: (block.subscribers ?? []).map((r) => {
      const row: SubscriberHealth = { id: r.id, connected: r.connected };
      if (r.last_pong_ms_ago != null) row.lastPongMsAgo = r.last_pong_ms_ago;
      if (r.offline_for_ms != null) row.offlineForMs = r.offline_for_ms;
      if (r.last_seen_ms_ago != null) row.lastSeenMsAgo = r.last_seen_ms_ago;
      return row;
    }),
  };
}
