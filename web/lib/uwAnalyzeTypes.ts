/**
 * TS mirrors of the FastAPI uw-analyze portfolio types.
 * Source of truth: scripts/api/services/uw_analyze_diff.py +
 * scripts/api/routes/uw_analyze.py.
 */

export type Source = "portfolio" | "watchlist" | "adhoc";

export type ChangeCode =
  | "GEX_FLIP_SIGN"
  | "MAX_PAIN_SHIFT"
  | "IV_RANK_JUMP"
  | "UNUSUAL_CALL_SWEEP"
  | "UNUSUAL_PUT_SWEEP";

export type Severity = "info" | "warn" | "alert";

export type Change = {
  code: ChangeCode;
  label: string;
  prev: number | string | null;
  curr: number | string | null;
  severity: Severity;
};

export type OiChange = {
  strike: number;
  side: "call" | "put";
  prev_oi: number;
  curr_oi: number;
  delta: number;
  delta_pct: number;
  label: string;
};

export type FlowDailyRow = {
  date: string;
  oi: number;
  mid: number;
  underlying_price: number;
  pct_change_premium: number;
};

export type FlowEvent = {
  id: string;
  ticker: string;
  side: "call" | "put";
  strike: number;
  expiry: string;
  detected_at: string;
  initial: {
    premium_usd: number;
    oi: number;
    volume: number;
    mid: number;
    underlying_price: number;
  };
  daily_track: FlowDailyRow[];
  status: "open" | "closed" | "anomaly" | "expired";
  anomaly_reason?: string | null;
  closed_at?: string | null;
};

export type UwGexStrikeRow = {
  strike: number;
  call_gamma?: number | null;
  put_gamma?: number | null;
  net_gamma: number | null;
  distance_pct?: number | null;
  is_call_wall?: boolean;
  is_put_wall?: boolean;
};

export type UwAnalyzeDisplay = {
  sector?: string | null;
  iv_rank?: number | null;
  iv?: number | null;
  rv?: number | null;
  call_wall_strike?: number | null;
  put_wall_strike?: number | null;
  gamma_per_1pct?: number | null;
  net_call_premium?: number | null;
  net_put_premium?: number | null;
  short_volume_ratio?: number | null;
  short_volume_trend?: number[] | null;
  term_structure_label?: "normal" | "inverted" | null;
  gex_flip?: number | null;
  gex_by_strike?: UwGexStrikeRow[] | null;
  max_pain?: number | null;
};

export type UwAnalyzeReport = {
  ticker: string;
  price: number | null;
  fetched_at: string;
  scores?: {
    bias?: string;
    grade?: string;
    composite?: number;
    market_structure?: number;
    volatility?: number;
    flow?: number;
    positioning?: number;
    mode?: string;
    skipped_buckets?: string[];
    reweighted?: boolean;
  };
  regime?: { gex_sign?: string; flip_distance_pct?: number | null };
  setup_thesis?: {
    structure_family?: string;
    regime?: string;
    bias?: string;
    rationale?: string;
  };
  notes?: string[];
  flow_alerts?: Record<string, unknown>[] | null;
};

export type UwSnapshot = {
  ticker: string;
  ts: string;
  report: UwAnalyzeReport;
  display: UwAnalyzeDisplay;
  derived: {
    gex_sign: "POSITIVE" | "NEGATIVE" | "NEUTRAL" | null;
    gex_flip_strike: number | null;
    max_pain: number | null;
    call_wall: number | null;
    put_wall: number | null;
    iv_rank: number | null;
    net_call_premium: number | null;
    net_put_premium: number | null;
    flow_score: number | null;
    spot: number | null;
  };
};

export type UwTickerRow = {
  ticker: string;
  sources: Source[];
  snapshot: UwSnapshot;
  prev_ts: string | null;
  changes: Change[];
  oi_changes: OiChange[];
  unusual_flow_events: FlowEvent[];
};

export type UwActionItem = {
  ticker: string;
  code: string;
  label: string;
  severity: "warn" | "alert";
};

export type UwPortfolioResponse = {
  fetched_at: string;
  market_state: "open" | "closed";
  ttl_seconds: number;
  tickers: UwTickerRow[];
  action_items: UwActionItem[];
};
