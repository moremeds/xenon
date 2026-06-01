export type NavSource = "reconstructed" | "reported";

export interface EquityCurvePoint {
  date: string;
  nav: number;
  source: NavSource;
  daily_pnl: number | null;
  recon: {
    delta: number;
    exceeded: boolean;
    notes: Record<string, unknown> | null;
  } | null;
}

export interface FutuTradeRow {
  futu_deal_id: string;
  ticker: string;
  action: "BUY" | "SELL";
  quantity: number;
  price: number;
  fees: number;
  realized_pnl: number | null;
  filled_at: string;
}

export interface FutuTradesPage {
  rows: FutuTradeRow[];
  next_cursor: string | null;
}

export interface FutuPerformance {
  total_return_twr: number;
  cagr: number;
  max_drawdown: number;
  win_rate: number | null;
  avg_holding_days: number | null;
  best_trade: unknown | null;
  worst_trade: unknown | null;
  total_commissions: number | null;
  monthly_pnl: Record<string, number>;
  external_cash_flows: { deposits: number; withdrawals: number };
}

export interface FutuReconciliationRow {
  kind: "daily" | "anomaly";
  recon_date: string;
  reported_nav: number | null;
  reconstructed_nav: number | null;
  delta: number | null;
  threshold: number | null;
  exceeded: boolean | null;
  notes: Record<string, unknown> | null;
  computed_at: string;
}

export interface FutuReconciliation {
  current_delta: number | null;
  rows: FutuReconciliationRow[];
}

export interface FutuSyncResult {
  status: string;
  fills_added: number;
  cash_flows_added: number;
  nav_rows_added: number;
  warnings: string[];
}

export interface CrossBrokerEquityCurve {
  per_broker: Record<
    "IB" | "FUTU",
    { date: string; nav: number; source: NavSource }[]
  >;
  combined: { date: string; nav: number }[] | null;
}
