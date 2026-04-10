/**
 * Ticker tier classification for the UW Analysis page.
 *
 * Six tiers drive the top-of-page grid:
 *   - indices     → SPY / QQQ / IWM / DIA (fixed order)
 *   - commodities → GLD / SLV
 *   - fixed       → TLT
 *   - vol         → UVXY
 *   - sector      → XLK / XLF / XLV / ... / SMH (alphabetical inside group)
 *   - single      → everything else (single-name equities, ad-hoc)
 *
 * The ticker universe covered by the first five tiers is the "scaffold":
 * these tiles render on first paint (before any backend data arrives) so
 * the page never shows an empty above-the-fold grid.
 */

import type { UwTickerRow } from "./uwAnalyzeTypes";

export const MARKET_INDICES = ["SPY", "QQQ", "IWM", "DIA"] as const;
export const COMMODITIES = ["GLD", "SLV"] as const;
export const FIXED_INCOME = ["TLT"] as const;
export const VOLATILITY = ["UVXY"] as const;
export const SECTOR_ETFS = [
  "XLK",
  "XLF",
  "XLV",
  "XLP",
  "XLE",
  "XLI",
  "XLB",
  "XLRE",
  "XLU",
  "XLC",
  "XLY",
  "SMH",
] as const;

/**
 * Sub-groups within the "single" tier (rendered under the "WATCH" header).
 * Order here = display order on the page.
 */
export const SINGLE_NAME_GROUPS: readonly {
  label: string;
  tickers: readonly string[];
}[] = [
  {
    label: "M7",
    tickers: ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"],
  },
  {
    label: "SEMIS",
    tickers: [
      "AMD",
      "AVGO",
      "INTC",
      "MU",
      "MRVL",
      "TSM",
      "QCOM",
      "CRWV",
      "NBIS",
      "IREN",
      "CRDO",
      "SNDK",
      "LITE",
      "GLW",
      "NOK",
      "TSEM",
    ],
  },
  {
    label: "GROWTH / TECH",
    tickers: [
      "PLTR",
      "HIMS",
      "HOOD",
      "SOFI",
      "ASTS",
      "RKLB",
      "NET",
      "PANW",
      "BKSY",
    ],
  },
  {
    label: "INDUSTRIALS",
    tickers: [
      "KO",
      "MCD",
      "LLY",
      "JPM",
      "GS",
      "BA",
      "COST",
      "WMT",
      "MS",
      "DAL",
      "XOM",
      "OXY",
      "CVX",
      "CRS",
      "FLY",
      "PL",
    ],
  },
  { label: "CRYPTO", tickers: ["COIN", "MSTR", "CRCL"] },
];

const SINGLE_GROUP_SETS = SINGLE_NAME_GROUPS.map(
  (g) => new Set(g.tickers.map((t) => t.toUpperCase())),
);

/** All single-name tickers that belong to a defined sub-group (for scaffold). */
export const WATCH_TICKERS: readonly string[] = SINGLE_NAME_GROUPS.flatMap(
  (g) => g.tickers,
);

/**
 * Split single-tier rows into ordered sub-groups + an OTHER bucket.
 * Within each sub-group, preserves the defined ticker order (changed-first
 * tickers still bubble up within their sub-group).
 */
export function groupSingleNames<
  T extends { ticker: string; changes?: unknown[] | null },
>(rows: readonly T[]): { label: string; rows: T[] }[] {
  const buckets: T[][] = SINGLE_NAME_GROUPS.map(() => []);
  const other: T[] = [];

  for (const row of rows) {
    const t = row.ticker.toUpperCase();
    let placed = false;
    for (let i = 0; i < SINGLE_GROUP_SETS.length; i++) {
      if (SINGLE_GROUP_SETS[i].has(t)) {
        buckets[i].push(row);
        placed = true;
        break;
      }
    }
    if (!placed) other.push(row);
  }

  const result: { label: string; rows: T[] }[] = [];
  for (let i = 0; i < SINGLE_NAME_GROUPS.length; i++) {
    if (buckets[i].length > 0) {
      result.push({
        label: SINGLE_NAME_GROUPS[i].label,
        rows: sortTier(buckets[i], "single"),
      });
    }
  }
  if (other.length > 0) {
    result.push({ label: "OTHER", rows: sortTier(other, "single") });
  }
  return result;
}

export type UwTier =
  | "indices"
  | "commodities"
  | "fixed"
  | "vol"
  | "sector"
  | "single";

const INDICES_SET: ReadonlySet<string> = new Set(MARKET_INDICES);
const COMMODITIES_SET: ReadonlySet<string> = new Set(COMMODITIES);
const FIXED_SET: ReadonlySet<string> = new Set(FIXED_INCOME);
const VOL_SET: ReadonlySet<string> = new Set(VOLATILITY);
const SECTOR_SET: ReadonlySet<string> = new Set(SECTOR_ETFS);

export function tierOf(ticker: string): UwTier {
  const t = ticker.toUpperCase();
  if (INDICES_SET.has(t)) return "indices";
  if (COMMODITIES_SET.has(t)) return "commodities";
  if (FIXED_SET.has(t)) return "fixed";
  if (VOL_SET.has(t)) return "vol";
  if (SECTOR_SET.has(t)) return "sector";
  return "single";
}

/** Fixed-order position of a ticker within its tier; Infinity for unknowns. */
function fixedOrderIndex(ticker: string, tier: UwTier): number {
  const t = ticker.toUpperCase();
  const list: readonly string[] | null =
    tier === "indices"
      ? MARKET_INDICES
      : tier === "commodities"
        ? COMMODITIES
        : tier === "fixed"
          ? FIXED_INCOME
          : tier === "vol"
            ? VOLATILITY
            : null;
  if (!list) return Number.POSITIVE_INFINITY;
  const i = list.indexOf(t);
  return i < 0 ? Number.POSITIVE_INFINITY : i;
}

/**
 * Sort rows within a tier.
 *   - indices: SPY/QQQ/IWM/DIA fixed; extras alphabetical at the end.
 *   - commodities/fixed/vol: fixed list order, then alphabetical.
 *   - sector: alphabetical.
 *   - single: changed-first, then alphabetical.
 */
export function sortTier<
  T extends { ticker: string; changes?: unknown[] | null },
>(rows: readonly T[], tier: UwTier): T[] {
  if (tier === "single") {
    const changed = rows.filter((r) => (r.changes?.length ?? 0) > 0);
    const unchanged = rows.filter((r) => (r.changes?.length ?? 0) === 0);
    const byTicker = (a: T, b: T) => a.ticker.localeCompare(b.ticker);
    return [...changed.sort(byTicker), ...unchanged.sort(byTicker)];
  }
  if (tier === "sector") {
    return [...rows].sort((a, b) => a.ticker.localeCompare(b.ticker));
  }
  return [...rows].sort((a, b) => {
    const ai = fixedOrderIndex(a.ticker, tier);
    const bi = fixedOrderIndex(b.ticker, tier);
    if (ai !== bi) return ai - bi;
    return a.ticker.localeCompare(b.ticker);
  });
}

export type UwTiered<T> = {
  indices: T[];
  commodities: T[];
  fixed: T[];
  vol: T[];
  sector: T[];
  single: T[];
};

export function groupByTier<
  T extends { ticker: string; changes?: unknown[] | null },
>(rows: readonly T[]): UwTiered<T> {
  const indices: T[] = [];
  const commodities: T[] = [];
  const fixed: T[] = [];
  const vol: T[] = [];
  const sector: T[] = [];
  const single: T[] = [];
  for (const row of rows) {
    switch (tierOf(row.ticker)) {
      case "indices":
        indices.push(row);
        break;
      case "commodities":
        commodities.push(row);
        break;
      case "fixed":
        fixed.push(row);
        break;
      case "vol":
        vol.push(row);
        break;
      case "sector":
        sector.push(row);
        break;
      default:
        single.push(row);
    }
  }
  return {
    indices: sortTier(indices, "indices"),
    commodities: sortTier(commodities, "commodities"),
    fixed: sortTier(fixed, "fixed"),
    vol: sortTier(vol, "vol"),
    sector: sortTier(sector, "sector"),
    single: sortTier(single, "single"),
  };
}

// ---------------------------------------------------------------------------
// Scaffold — static universe rendered on first paint before the backend
// portfolio resolves. Every row satisfies the strict `UwTickerRow` shape so
// it can be merged alongside live data.
// ---------------------------------------------------------------------------

export const SCAFFOLD_TICKERS: readonly string[] = [
  ...MARKET_INDICES,
  ...COMMODITIES,
  ...FIXED_INCOME,
  ...VOLATILITY,
  ...SECTOR_ETFS,
  ...WATCH_TICKERS,
];

export function makeScaffoldRow(ticker: string): UwTickerRow {
  return {
    ticker,
    sources: [],
    prev_ts: null,
    changes: [],
    oi_changes: [],
    unusual_flow_events: [],
    snapshot: {
      ticker,
      ts: "",
      report: {
        ticker,
        price: null,
        fetched_at: "",
      },
      display: {},
      derived: {
        gex_sign: null,
        gex_flip_strike: null,
        max_pain: null,
        call_wall: null,
        put_wall: null,
        iv_rank: null,
        net_call_premium: null,
        net_put_premium: null,
        flow_score: null,
        spot: null,
      },
    },
  };
}

export const SCAFFOLD_ROWS: readonly UwTickerRow[] =
  SCAFFOLD_TICKERS.map(makeScaffoldRow);

/**
 * A scaffold row is one that has not yet been populated by a backend scan.
 * Check both the snapshot timestamp and report timestamp because either
 * sentinel ("") is only emitted by `makeScaffoldRow`.
 */
export function isScaffold(row: UwTickerRow): boolean {
  return row.snapshot.ts === "" && row.snapshot.report.fetched_at === "";
}

/**
 * Merge a live portfolio response with the static scaffold.
 *
 *   - Every scaffold ticker renders, preferring the live row if one exists.
 *   - Live-only rows (ad-hoc, watchlist single names, portfolio positions
 *     that aren't in the static universe) append at the end.
 *   - Ordering within each tier is handled later by `groupByTier`.
 */
export function mergeScaffoldWithLive(
  scaffold: readonly UwTickerRow[],
  live: readonly UwTickerRow[],
): UwTickerRow[] {
  const liveMap = new Map<string, UwTickerRow>();
  for (const row of live) liveMap.set(row.ticker.toUpperCase(), row);

  const scaffoldTickerSet = new Set(
    scaffold.map((r) => r.ticker.toUpperCase()),
  );

  const merged: UwTickerRow[] = scaffold.map((stub) => {
    const hit = liveMap.get(stub.ticker.toUpperCase());
    return hit ?? stub;
  });

  for (const row of live) {
    if (!scaffoldTickerSet.has(row.ticker.toUpperCase())) {
      merged.push(row);
    }
  }
  return merged;
}
