import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { resolveAccountDayPnlValue } from "@/components/MetricCards";

/**
 * Per-account dashboard metrics, and a sign-preserving merge across accounts
 * (IB + FUTU). Pure helpers so the merge math is unit-testable independent of
 * the React tree.
 *
 * Today P&L is broker-aware via resolveAccountDayPnlValue(): IB's streamed
 * daily_pnl, FUTU's intraday-from-live-prices. Sign is preserved throughout
 * (web/CLAUDE.md credit/debit convention) — no Math.abs.
 *
 * NOTE: assumes both accounts report in USD (the existing UI formats FUTU with
 * "$"). If a non-USD FUTU account is ever surfaced, this sum must convert first.
 */
export type AccountMetrics = {
  netLiq: number | null;
  todayPnl: number | null;
  openRisk: number | null;
  cash: number | null;
};

export function accountMetrics(
  portfolio: PortfolioData | null,
  prices?: Record<string, PriceData>,
): AccountMetrics {
  const acct = portfolio?.account_summary;
  return {
    netLiq: acct?.net_liquidation ?? null,
    todayPnl: portfolio ? resolveAccountDayPnlValue(portfolio, prices) : null,
    openRisk: portfolio?.total_deployed_dollars ?? null,
    cash: acct?.cash ?? acct?.settled_cash ?? null,
  };
}

const FIELDS = ["netLiq", "todayPnl", "openRisk", "cash"] as const;

/**
 * Sum each field across accounts. A null component is SKIPPED (not treated as
 * zero, which would silently hide a missing account). A field is null only when
 * every account is null for it.
 */
export function mergeAccountMetrics(rows: AccountMetrics[]): AccountMetrics {
  const out: AccountMetrics = {
    netLiq: null,
    todayPnl: null,
    openRisk: null,
    cash: null,
  };
  for (const field of FIELDS) {
    let sum = 0;
    let any = false;
    for (const row of rows) {
      const v = row[field];
      if (v != null && Number.isFinite(v)) {
        sum += v;
        any = true;
      }
    }
    out[field] = any ? sum : null;
  }
  return out;
}
