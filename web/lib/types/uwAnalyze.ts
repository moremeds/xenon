/**
 * UW Analyse — TypeScript mirror of Pydantic models in
 * `scripts/api/routes/uw_analyze.py`. Manually kept in sync.
 *
 * Snapshot test: `web/tests/uw-analyze.route.test.ts` asserts shape
 * against the fixture used by the FastAPI pytest in
 * `scripts/tests/test_uw_analyze_route.py`.
 */

export interface UwGexStrikeRow {
  strike: number;
  call_gamma: number | null;
  put_gamma: number | null;
  net_gamma: number | null;
  distance_pct: number | null;
  is_call_wall: boolean;
  is_put_wall: boolean;
}

export interface UwAnalyzeDisplay {
  sector: string | null;
  iv_rank: number | null;
  iv: number | null;
  rv: number | null;
  call_wall_strike: number | null;
  put_wall_strike: number | null;
  gamma_per_1pct: number | null;
  net_call_premium: number | null;
  net_put_premium: number | null;
  short_volume_ratio: number | null;
  short_volume_trend: number[] | null;
  term_structure_label: "normal" | "inverted" | null;
  gex_flip: number | null;
  gex_by_strike: UwGexStrikeRow[] | null;
}

export interface UwAnalyzeReportScores {
  market_structure: number;
  volatility: number;
  flow: number;
  positioning: number;
  composite: number;
  grade: "A" | "B" | "C";
  bias: "STRONGLY_BULLISH" | "BULLISH" | "MIXED" | "BEARISH" | "STRONGLY_BEARISH";
  mode: "full" | "fast";
  reweighted: boolean;
  skipped_buckets: string[];
}

export interface UwAnalyzeReport {
  ticker: string;
  price: number | null;
  fetched_at: string;
  data_freshness: Record<string, string>;
  scores: UwAnalyzeReportScores;
  notes: string[];
  setup_thesis: {
    bias: string;
    regime: string;
    structure_family: string;
    rationale: string;
  } | null;
  // Loose-typed pass-throughs from Python dataclasses (vrp, regime, benchmark)
  // — strict typing not needed for v1 since the UI only reads a few fields.
  vrp?: Record<string, unknown>;
  regime?: Record<string, unknown>;
  benchmark?: Record<string, unknown>;
}

export interface UwAnalyzeResponse {
  report: UwAnalyzeReport;
  display: UwAnalyzeDisplay;
  generated_at: string;
}
