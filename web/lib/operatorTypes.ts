export type IbAuthVerdict =
  | "authenticated"
  | "awaiting"
  | "unreachable"
  | "unknown";

export type IbGatewayInfo = {
  port_listening: boolean;
  upstream_dead?: boolean;
  service_state?: string;
  host?: string;
  port?: number;
  gateway_mode?: string;
};

export type IbPoolRole = { connected: boolean; client_id?: number };

export type SnapshotterInfo = {
  last_write_at: string | null;
  stale_seconds: number | null;
};

export type OrderSubmissionsInfo = {
  unknown_count: number | null;
  alarm: boolean;
};

export type FlexDivergenceInfo = {
  configured: boolean;
  ran_at?: string | null;
  divergence_count?: number | null;
  total_compared?: number | null;
};

export type RealtimeSubscribersInfo = {
  reachable: boolean;
  ib_connected?: boolean | null;
  subscribers: unknown[];
  anonymous_count: number;
  ttl_ms?: number | null;
};

export type FutuInfo = {
  configured: boolean;
  connected: boolean;
  last_sync_at?: string | null;
  last_sync_age_s?: number | null;
};

export type UwInfo = {
  bucket_hour: string;
  requests: number;
  cache_hits: number;
  status_2xx: number;
  status_4xx: number;
  status_5xx: number;
  latency_avg_ms: number | null;
} | null;

// Live UW rate-limit / quota snapshot, sourced from the x-uw-* response
// headers on a Next-side probe (UW_TOKEN lives in web/.env).
export type UwQuota = {
  configured: boolean;
  daily_count: number | null; // x-uw-daily-req-count
  daily_limit: number | null; // x-uw-token-req-limit
  minute_count: number | null; // x-uw-minute-req-counter
  minute_remaining: number | null; // x-uw-req-per-minute-remaining
  minute_reset_ms: number | null; // x-uw-req-per-minute-reset
  fetched_at: string | null;
  error?: string;
};

export type WriterRow = {
  service: string;
  state: string; // "ok" | "error" | "syncing" | "paused" | "missing"
  detail: string | null;
  last_error: string | null;
  last_started_at: string | null;
  last_finished_at: string | null;
  updated_at: string | null;
  age_secs: number | null;
};

export type OperatorData = {
  generated_at: string;
  ib_gateway: IbGatewayInfo;
  ib_pool: Record<string, IbPoolRole>;
  ib_auth: IbAuthVerdict;
  trading_mode: string;
  account: string;
  mode_verified: boolean;
  snapshotter: SnapshotterInfo;
  order_submissions: OrderSubmissionsInfo;
  flex_divergence: FlexDivergenceInfo;
  realtime_subscribers: RealtimeSubscribersInfo;
  futu: FutuInfo;
  uw: UwInfo;
  writers: WriterRow[];
};
