/**
 * Futu → PortfolioData adapter.
 *
 * Converts the raw JSON emitted by `scripts/clients/futu_client.py`
 * (fetch_portfolio envelope) into the exact shape `MetricCards` and
 * `WorkspaceSections` already consume for IB.
 *
 * Design rule: no Futu-specific UI code. Every component downstream
 * sees a normal `PortfolioData` regardless of source. The only divergence
 * from IB is that some fields are `null` (Dividends, Prev EWL, Reg T, SMA,
 * Kelly, entry_date) because Futu's API doesn't expose them.
 */

import type {
  AccountSummary,
  PortfolioData,
  PortfolioLeg,
  PortfolioPosition,
} from "@/lib/types";

// ── Raw shape from scripts/clients/futu_client.py ──────────────────────────

export type FutuNormalizedStock = {
  kind: "STK";
  symbol: string;
  exchange: string;
  currency: string;
  market?: string; // Futu market prefix ("US", "JP", ...) — foreign stocks set it
  live_data: boolean;
};

export type FutuNormalizedOption = {
  kind: "OPT";
  symbol: string;
  expiry: string; // YYYYMMDD
  strike: number;
  right: "C" | "P";
  exchange: string;
  currency: string;
  trading_class: string | null;
  live_data: boolean;
};

export type FutuNormalizedUnknown = {
  kind: "UNKNOWN";
  raw: string;
  reason: string;
  live_data: boolean;
};

export type FutuNormalized =
  | FutuNormalizedStock
  | FutuNormalizedOption
  | FutuNormalizedUnknown;

export type FutuRawPosition = {
  futu_code: string;
  normalized: FutuNormalized;
  quantity: number;
  avg_cost: number;
  market_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  currency: string;
  position_side: string;
};

export type FutuRawAccountSummary = {
  net_liquidation: number | null;
  equity_with_loan: number | null;
  cash: number | null;
  settled_cash: number | null;
  buying_power: number | null;
  available_funds: number | null;
  initial_margin: number | null;
  maintenance_margin: number | null;
  excess_liquidity: number | null;
  gross_position_value: number | null;
  unrealized_pnl: number | null;
  daily_pnl: number | null;
  realized_pnl: number | null;
  dividends: number | null;
  previous_day_ewl: number | null;
  reg_t_equity: number | null;
  sma: number | null;
};

export type FutuPortfolioEnvelope = {
  ok?: true;
  fetched_at: string;
  data_as_of: string;
  account_id: string | null;
  source: "futu";
  is_stale: boolean;
  warnings: string[];
  positions: FutuRawPosition[];
  count: number;
  account_summary: FutuRawAccountSummary;
  account_raw?: Record<string, unknown>;
};

/**
 * First-boot shape returned by `GET /futu/portfolio` when the server has
 * never written a snapshot yet. HTTP 200 (not 404) per tribunal T14 — the
 * UI distinguishes "never synced" from "sync failed" so it can prompt
 * the user to click the sync button without flashing an error state.
 */
export type FutuNeverSynced = {
  ok: false;
  code: "never_synced";
  positions: [];
  count: 0;
  account_summary: null;
  fetched_at: null;
  data_as_of: null;
};

export type FutuPortfolioResponse = FutuPortfolioEnvelope | FutuNeverSynced;

export function isFutuNeverSynced(
  x: FutuPortfolioResponse,
): x is FutuNeverSynced {
  return (
    (x as FutuNeverSynced).ok === false &&
    (x as FutuNeverSynced).code === "never_synced"
  );
}

// ── Adapter ────────────────────────────────────────────────────────────────

/**
 * Convert a Futu envelope into a PortfolioData that MetricCards +
 * WorkspaceSections can render without branching on broker.
 */
export function futuToPortfolioData(env: FutuPortfolioEnvelope): PortfolioData {
  const positions: PortfolioPosition[] = env.positions.map((p, idx) =>
    futuPositionToPortfolioPosition(p, idx),
  );

  // OPEN RISK / deployed capital must be USD. Per-position market_value is in
  // each security's NATIVE currency, so summing rows mixes units (one ¥2.45M
  // JPY row inflated this to a bogus $2.9M open-risk headline). The envelope's
  // gross_position_value is sourced from Futu's accinfo_query(currency=USD) and
  // is already USD-denominated — use it. Fall back to the native sum only when
  // the backend omits it (pre-USD-fix snapshots).
  const grossUsd = env.account_summary.gross_position_value;
  const totalDeployedDollars =
    grossUsd != null && Number.isFinite(grossUsd)
      ? Math.abs(grossUsd)
      : positions.reduce((sum, p) => sum + Math.abs(p.market_value ?? 0), 0);
  const bankroll = env.account_summary.net_liquidation ?? 0;
  const totalDeployedPct =
    bankroll > 0 ? (totalDeployedDollars / bankroll) * 100 : 0;

  const definedCount = positions.filter(
    (p) => p.risk_profile === "defined",
  ).length;
  const undefinedCount = positions.filter(
    (p) => p.risk_profile === "undefined",
  ).length;

  return {
    source: "futu",
    bankroll,
    peak_value: bankroll, // Futu doesn't track peak; use current NLV
    last_sync: env.fetched_at,
    positions,
    total_deployed_dollars: totalDeployedDollars,
    total_deployed_pct: totalDeployedPct,
    remaining_capacity_pct: Math.max(0, 100 - totalDeployedPct),
    position_count: positions.length,
    defined_risk_count: definedCount,
    undefined_risk_count: undefinedCount,
    avg_kelly_optimal: null, // Futu positions have no Kelly sizing
    account_summary: futuToAccountSummary(env.account_summary),
  };
}

/** Map the pre-computed aggregate envelope onto the UI's AccountSummary shape. */
export function futuToAccountSummary(s: FutuRawAccountSummary): AccountSummary {
  return {
    net_liquidation: s.net_liquidation ?? 0,
    daily_pnl: s.daily_pnl, // already nullable
    unrealized_pnl: s.unrealized_pnl ?? 0,
    realized_pnl: s.realized_pnl ?? 0,
    settled_cash: s.settled_cash ?? 0,
    maintenance_margin: s.maintenance_margin ?? 0,
    excess_liquidity: s.excess_liquidity ?? 0,
    buying_power: s.buying_power ?? 0,
    dividends: s.dividends, // null → card renders ---
    cash: s.cash ?? undefined,
    initial_margin: s.initial_margin ?? undefined,
    available_funds: s.available_funds ?? undefined,
    equity_with_loan: s.equity_with_loan ?? undefined,
    previous_day_ewl: s.previous_day_ewl ?? undefined,
    reg_t_equity: s.reg_t_equity ?? undefined,
    sma: s.sma ?? undefined,
    gross_position_value: s.gross_position_value ?? undefined,
  };
}

// ── Position conversion ────────────────────────────────────────────────────

function futuPositionToPortfolioPosition(
  p: FutuRawPosition,
  idx: number,
): PortfolioPosition {
  const {
    structure,
    structureType,
    riskProfile,
    type,
    strike,
    expiry,
    ticker,
  } = classifyFutuPosition(p);

  const direction: "LONG" | "SHORT" = p.quantity >= 0 ? "LONG" : "SHORT";
  const contracts = Math.abs(p.quantity);
  const multiplier = type === "Stock" ? 1 : 100;
  // Preserve the opening cashflow sign so downstream P&L math stays correct:
  // long positions pay debit (+entry cost), short positions collect credit
  // (-entry cost). Futu already signs market_value this way.
  const entryCost = p.avg_cost * p.quantity * multiplier;

  // Native trading currency (JPY/HKD/USD). MUST be propagated: the FX-aware
  // display path (PositionTable, PortfolioByStructure) and the forex
  // subscription (deriveFxSubscriptions) both key off pos.currency. Dropping it
  // made every Futu row default to USD, so a ¥/₩ market_value rendered as $.
  const currency = (p.currency || "USD").toUpperCase();

  const leg: PortfolioLeg = {
    direction,
    contracts,
    type,
    strike,
    currency,
    entry_cost: entryCost,
    avg_cost: p.avg_cost,
    market_price: p.market_price || null,
    market_value: p.market_value,
    market_price_is_calculated: false,
  };

  return {
    id: idx + 1,
    ticker,
    structure,
    structure_type: structureType,
    risk_profile: riskProfile,
    currency,
    expiry,
    contracts,
    direction,
    entry_cost: entryCost,
    max_risk: null, // Futu doesn't provide this; Xenon's risk calculator is IB-specific
    market_value: p.market_value,
    legs: [leg],
    ib_daily_pnl: null, // no IB per-position P&L available for Futu rows
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "", // Futu positions snapshot does not include entry date
  };
}

type Classification = {
  ticker: string;
  structure: string;
  structureType: string;
  riskProfile: "equity" | "defined" | "undefined" | "complex";
  type: "Call" | "Put" | "Stock";
  strike: number | null;
  expiry: string;
};

function classifyFutuPosition(p: FutuRawPosition): Classification {
  const n = p.normalized;
  const isLong = p.quantity >= 0;

  if (n.kind === "STK") {
    return {
      ticker: n.symbol,
      structure: "Stock",
      structureType: "Stock",
      riskProfile: "equity",
      type: "Stock",
      strike: null,
      expiry: "",
    };
  }

  if (n.kind === "OPT") {
    const rightWord = n.right === "C" ? "Call" : "Put";
    const dirWord = isLong ? "Long" : "Short";
    return {
      ticker: n.symbol,
      structure: `${dirWord} ${rightWord}`,
      structureType: `${dirWord} ${rightWord}`,
      // Long option = defined risk (premium paid is max loss).
      // Short option = undefined risk at this layer; the Naked Short Guard
      // is an ORDER-time check, not a portfolio classifier. A short option
      // sitting in the Futu account is reported honestly as undefined-risk
      // regardless of whether a matching long covers it (structure-level
      // classification for multi-leg combos is IB-sync-specific and not
      // reimplemented for Futu in v1).
      riskProfile: isLong ? "defined" : "undefined",
      type: rightWord,
      strike: n.strike,
      expiry: formatExpiry(n.expiry),
    };
  }

  // UNKNOWN — render with the raw futu code as ticker and a warning tag.
  return {
    ticker: n.raw || p.futu_code,
    structure: "Unknown",
    structureType: "Unknown",
    riskProfile: "complex",
    type: "Stock",
    strike: null,
    expiry: "",
  };
}

/** `YYYYMMDD` → `YYYY-MM-DD` to match the IB-side format. */
function formatExpiry(yyyymmdd: string): string {
  if (!yyyymmdd || yyyymmdd.length !== 8) return yyyymmdd || "";
  return `${yyyymmdd.slice(0, 4)}-${yyyymmdd.slice(4, 6)}-${yyyymmdd.slice(6, 8)}`;
}
