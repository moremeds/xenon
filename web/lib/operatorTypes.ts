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

// IB Flex Web Service token expiry (GET /health + /admin/operator `flex_token`).
export type FlexTokenInfo = {
  configured: boolean;
  expires_at?: string;
  days_remaining?: number;
  reminder_days?: number[];
  active_threshold?: number | null;
  should_warn?: boolean;
  expired?: boolean;
  error?: string;
};

export type RealtimeSubscribersInfo = {
  reachable: boolean;
  ib_connected?: boolean | null;
  subscribers: unknown[];
  anonymous_count: number;
  ttl_ms?: number | null;
  // Futu quote feed health; absent on older backends.
  futu_quotes?: FutuQuotesInfo | null;
};

export type FutuQuotesInfo = {
  connected: boolean; // relay <-> futu-quotes socket
  opend_connected: boolean;
  quota: {
    ceiling: number;
    own_used: number;
    all_used: number | null;
    remain: number | null;
  } | null;
  routes: { futu: number; ib: number }; // live symbol subscriptions per source
};

export const FUTU_HELD_KINDS = [
  "FUTU_FILL_UNPRICED",
  "FUTU_FILL_CONFLICT",
  "FUTU_NOT_OBSERVED",
] as const;
export type FutuHeldKind = (typeof FUTU_HELD_KINDS)[number];
export type FutuHeldRows = Partial<
  Record<FutuHeldKind, { count: number; order_ids: string[] }>
>;
export type FutuObservation = { as_of: string | null; complete: boolean };

export type FutuScope = {
  account_env: string; // "paper" | "live"
  configured?: boolean;
  last_sync_at?: string | null;
  last_sync_age_s?: number | null;
  observation?: FutuObservation | null;
  scope_observation_loop?: boolean;
  held_rows?: FutuHeldRows | null;
};

export type FutuCapabilities = Record<
  string,
  { tuples: string[]; modify: string[] }
>;

// Top-level fields describe the API's own scope (`scope_env`); `scopes` lists
// every Futu env. All new fields are optional — older backends omit them.
export type FutuInfo = {
  configured: boolean;
  connected: boolean;
  last_sync_at?: string | null;
  last_sync_age_s?: number | null;
  observation?: FutuObservation | null;
  held_rows?: FutuHeldRows | null;
  scope_env?: string;
  trading?: Record<string, boolean>;
  capabilities?: FutuCapabilities;
  scopes?: FutuScope[];
};

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
  state: string; // "ok" | "error" | "paused" | "missing"
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
  flex_token?: FlexTokenInfo;
  realtime_subscribers: RealtimeSubscribersInfo;
  futu: FutuInfo;
  writers: WriterRow[];
};
