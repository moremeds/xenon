export type PriceData = {
  symbol: string;
  last: number | null;
  lastIsCalculated: boolean;
  bid: number | null;
  ask: number | null;
  bidSize: number | null;
  askSize: number | null;
  volume: number | null;
  high: number | null;
  low: number | null;
  open: number | null;
  close: number | null;
  // Misc Stats (generic tick 165)
  week52High: number | null;
  week52Low: number | null;
  avgVolume: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  impliedVol: number | null;
  undPrice: number | null;
  timestamp: string;
  /** Relay-stamped when the row is older than the stale threshold (Futu rows;
   *  absent on fresh rows and on IB-routed data). */
  stale?: boolean;
  /** Quote provider — present only on Futu-routed rows. */
  source?: "futu";
  /** Futu extended-session marker for US stock rows: pre-market, after-hours,
   *  or overnight (Sun–Thu ET). Absent on RTH rows. */
  session?: "pre" | "after" | "overnight";
};

export type FundamentalsData = {
  symbol: string;
  peRatio: number | null;
  eps: number | null;
  dividendYield: number | null;
  week52High: number | null;
  week52Low: number | null;
  priceBookRatio: number | null;
  roe: number | null;
  revenue: number | null;
  timestamp: string;
};

export type WSPriceMessage = {
  type: "price";
  symbol: string;
  data: PriceData;
};

export type WSSubscribedMessage = {
  type: "subscribed";
  symbols: string[];
};

export type WSUnsubscribedMessage = {
  type: "unsubscribed";
  symbols: string[];
};

export type WSSnapshotMessage = {
  type: "snapshot";
  symbol: string;
  data: PriceData;
};

export type WSFundamentalsMessage = {
  type: "fundamentals";
  symbol: string;
  data: FundamentalsData;
};

export type WSErrorMessage = {
  type: "error";
  message: string;
};

export type WSPingMessage = {
  type: "ping";
};

export type WSPongMessage = {
  type: "pong";
};

export type WSStatusMessage = {
  type: "status";
  ib_connected: boolean;
  ib_issue: string | null;
  ib_status_message: string | null;
  subscriptions: string[];
};

export type WSBatchMessage = {
  type: "batch";
  updates: Record<string, PriceData>;
};

/* ─── Depth-of-book (L2) types ─────────────────────────── */

export type DepthSide = "bid" | "ask";

/** One book row. marketMaker/exchange null for futures (no venue attribution). */
export type DepthLevel = {
  price: number;
  size: number;
  marketMaker: string | null; // MPID (NASDAQ TotalView) — equities direct
  exchange: string | null; // venue code (SMART equities, options BBO)
  /**
   * Options only: this venue row sets the NBBO (best bid / best ask across
   * exchanges). The relay flags it per row; ties at the inside mark every
   * matching venue. Absent on stock/future books, which key best on position.
   */
  nbbo?: boolean;
};

/**
 * Cross-venue NBBO summary for an option montage. The relay derives this from
 * the inside (best bid / best ask) venue rows and attaches it to option books
 * only — it is the authoritative top-of-book for the header on the Book tab.
 */
export type DepthNbbo = {
  bestBid: number | null;
  bestAsk: number | null;
  mid: number | null;
  bidSize: number;
  askSize: number;
};

export type DepthBook = {
  symbol: string; // same keyspace as PriceData.symbol (ticker | optionKey | future)
  kind: "stock" | "option" | "future";
  bid: DepthLevel[]; // index 0 = inside/best
  ask: DepthLevel[]; // index 0 = inside/best
  isSmartDepth: boolean; // true equities/options, false futures
  feed: string | null; // head-pill label, e.g. "SMART DEPTH · TOTALVIEW"
  entitled: boolean; // false → render L1 fallback
  /** Options only: cross-venue NBBO summary derived by the relay. */
  nbbo?: DepthNbbo;
  timestamp: string;
};

/** Time & Sales tape row. */
export type Trade = {
  price: number;
  size: number;
  exchange: string | null;
  time: string;
};

export type WSDepthMessage = {
  type: "depth";
  symbol: string;
  data: DepthBook;
};

/**
 * ⚠️ DEPTH-KEY contract: the `updates` keys here (and `symbol` on
 * `WSDepthUnavailableMessage` / `DepthBook.symbol`) carry the **depth key** —
 * the composite `SYMBOL_YYYYMMDD_STRIKE_RIGHT` (`optionKey`) for options, the
 * bare symbol for stocks/futures. This is the same key `usePrices` uses for its
 * `depths`/`tape` state (`bookKey`). Using the bare underlying for an option
 * would clear/mark the wrong entry. Backend (`hydrateAndBroadcastDepth`) and
 * frontend (`applyDepthMessage`) must never drift from this.
 */
export type WSDepthBatchMessage = {
  type: "depth-batch";
  updates: Record<string, DepthBook>;
};

export type WSDepthUnavailableMessage = {
  type: "depth-unavailable";
  symbol: string;
  reason: "no-entitlement" | "futures-no-depth" | "recycled";
  code?: number;
};

/**
 * Time & Sales batch. Rides the SAME `subscribe-depth` as the depth channel
 * (the relay seeds the tape for the focused depth symbol — no separate client
 * action). Trades are newest-first within each symbol's array; the hook merges
 * and bounds to the most recent rows per symbol. Keyed by the depth key above.
 */
export type WSTapeBatchMessage = {
  type: "tape-batch";
  updates: Record<string, Trade[]>;
};

export type WSMessage =
  | WSPriceMessage
  | WSFundamentalsMessage
  | WSSubscribedMessage
  | WSUnsubscribedMessage
  | WSSnapshotMessage
  | WSBatchMessage
  | WSErrorMessage
  | WSPingMessage
  | WSPongMessage
  | WSStatusMessage
  | WSDepthMessage
  | WSDepthBatchMessage
  | WSDepthUnavailableMessage
  | WSTapeBatchMessage;

/* ─── Option contract types & helpers ─────────────────── */

export type OptionContract = {
  symbol: string;
  expiry: string; // YYYYMMDD
  strike: number;
  right: "C" | "P";
  /** Subscription priority tier for the quote relay (contract-x7 D9):
   *  "held" for portfolio/order legs, "view" for ticker-detail chain legs.
   *  Absent = held. */
  tier?: SubscriptionTier;
};

/** Quote-subscription priority tier. Absent tier = "held" at the relay. */
export type SubscriptionTier = "held" | "view";

/** Subscribe-frame symbol entry: a bare ticker, or {symbol, tier}. */
export type SymbolSubscription =
  | string
  | { symbol: string; tier: SubscriptionTier };

export function normalizeOptionExpiry(expiry: string): string | null {
  const compact = expiry.trim().replace(/-/g, "");
  return compact.length === 8 ? compact : null;
}

export function normalizeOptionContract(
  contract: OptionContract,
): OptionContract | null {
  const symbol = contract.symbol.trim().toUpperCase();
  const expiry = normalizeOptionExpiry(contract.expiry);
  if (
    !symbol ||
    !expiry ||
    !Number.isFinite(contract.strike) ||
    contract.strike <= 0
  ) {
    return null;
  }
  return {
    symbol,
    expiry,
    strike: contract.strike,
    right: contract.right,
    ...(contract.tier ? { tier: contract.tier } : {}),
  };
}

/** Build composite key for an option contract: SYMBOL_YYYYMMDD_STRIKE_RIGHT */
export function optionKey(c: OptionContract): string {
  const normalized = normalizeOptionContract(c);
  if (normalized) {
    return `${normalized.symbol}_${normalized.expiry}_${normalized.strike}_${normalized.right}`;
  }
  return `${c.symbol.trim().toUpperCase()}_${c.expiry.trim()}_${c.strike}_${c.right}`;
}

/**
 * Inverse of {@link optionKey}: parse a composite `SYMBOL_YYYYMMDD_STRIKE_RIGHT`
 * key back into an OptionContract. The symbol may itself contain underscores, so
 * we split from the RIGHT: last segment = right (C/P), then strike, then expiry,
 * and everything before that is the symbol. Returns null for non-option keys
 * (bare stock symbols, malformed input) — the caller treats those as stocks.
 */
export function parseOptionKey(key: string): OptionContract | null {
  const parts = key.trim().split("_");
  if (parts.length < 4) return null;
  const right = parts[parts.length - 1];
  if (right !== "C" && right !== "P") return null;
  const strike = Number(parts[parts.length - 2]);
  const expiry = parts[parts.length - 3];
  const symbol = parts.slice(0, parts.length - 3).join("_");
  return normalizeOptionContract({ symbol, expiry, strike, right });
}

export function uniqueOptionContracts(
  contracts: OptionContract[],
): OptionContract[] {
  const idxByKey = new Map<string, number>();
  const normalizedContracts: OptionContract[] = [];
  for (const contract of contracts) {
    const normalized = normalizeOptionContract(contract);
    if (!normalized) continue;
    const key = optionKey(normalized);
    const prevIdx = idxByKey.get(key);
    if (prevIdx != null) {
      // held > view: a held or bare (absent = held) duplicate replaces a
      // kept "view" tag on the same contract.
      if (normalized.tier !== "view") {
        const { tier: _keptTier, ...rest } = normalizedContracts[prevIdx];
        normalizedContracts[prevIdx] = normalized.tier
          ? { ...rest, tier: normalized.tier }
          : rest;
      }
      continue;
    }
    idxByKey.set(key, normalizedContracts.length);
    normalizedContracts.push(normalized);
  }
  return normalizedContracts;
}

/** Stable hash for a list of option contracts (for memoization change detection) */
export function contractsKey(contracts: OptionContract[]): string {
  return uniqueOptionContracts(contracts).map(optionKey).sort().join(",");
}

/**
 * Convert a portfolio leg into an IB-ready OptionContract descriptor.
 * Returns null for Stock legs, null/0 strikes, or missing data.
 */
export function portfolioLegToContract(
  ticker: string,
  expiry: string,
  leg: { type: string; strike: number | null },
): OptionContract | null {
  if (leg.type === "Stock") return null;
  if (leg.strike == null || leg.strike === 0) return null;
  if (!expiry || expiry === "N/A") return null;

  const right = leg.type === "Call" ? "C" : leg.type === "Put" ? "P" : null;
  if (!right) return null;

  return normalizeOptionContract({
    symbol: ticker.toUpperCase(),
    expiry,
    strike: leg.strike,
    right,
  });
}

/* ─── Index contract types ────────────────────────────── */

export type IndexContract = {
  symbol: string;
  exchange: string; // e.g. "CBOE"
};

/* ─── Forex + foreign-stock contract types ────────────── */

/** A forex pair for live FX conversion. base=USD, quote=JPY → "USD.JPY". */
export type ForexContract = {
  base: string;
  quote: string;
};

/** A foreign cash-equity with its native venue + currency (TSEJ/JPY, KRX/KRW). */
export type StockMeta = {
  symbol: string;
  exchange: string;
  currency: string;
};

/** Relay key for a forex pair: "<BASE>.<QUOTE>" (uppercased), e.g. "USD.JPY". */
export function forexKey(c: ForexContract): string {
  return `${c.base.trim().toUpperCase()}.${c.quote.trim().toUpperCase()}`;
}

/** Stable, order-independent hash of a forex list (memo change detection). */
export function forexesKey(list: ForexContract[]): string {
  return [...list.map(forexKey)].sort().join(",");
}

/** Stable hash of a foreign-stock list, keyed by bare uppercased symbol. */
export function stocksMetaKey(list: StockMeta[]): string {
  return [...list.map((s) => s.symbol.trim().toUpperCase())].sort().join(",");
}

/* ─── Symbol helpers ──────────────────────────────────── */

export function normalizeSymbolList(symbols: string[]): string[] {
  return [...symbols]
    .map((symbol) => symbol.trim().toUpperCase())
    .filter((symbol) => symbol.length > 0);
}

export function symbolKey(symbols: string[]): string {
  return normalizeSymbolList(symbols).sort().join(",");
}

/**
 * Diff key for a subscribe-entry: bare symbols key on the symbol itself;
 * tiered entries append `:<tier>` so a tier-only change differs (':' never
 * appears in tickers, forex keys, or option keys).
 */
export function symbolEntryKey(entry: SymbolSubscription): string {
  return typeof entry === "string"
    ? entry
    : `${entry.symbol}:${entry.tier}`;
}

/** Diff key for a contract entry — `optionKey` + `:<tier>` when tagged. */
export function contractEntryKey(contract: OptionContract): string {
  const key = optionKey(contract);
  return contract.tier ? `${key}:${contract.tier}` : key;
}

/** Bare identity of a diff key produced by {@link symbolEntryKey} or
 *  {@link contractEntryKey} — strips a trailing `:<tier>`. */
export function stripEntryTier(key: string): string {
  return key.replace(/:(held|view)$/, "");
}

/**
 * Normalize + dedup a list of symbol subscribe-entries. Duplicate symbols
 * keep the first entry, upgraded to held when any copy is not "view" (a bare
 * string means held at the relay). Output entries are bare strings unless
 * they carry a tier.
 */
export function normalizeSymbolSubscriptions(
  entries: SymbolSubscription[],
): SymbolSubscription[] {
  const out: SymbolSubscription[] = [];
  const idxBySymbol = new Map<string, number>();
  for (const entry of entries) {
    const symbol = (typeof entry === "string" ? entry : entry.symbol)
      .trim()
      .toUpperCase();
    if (!symbol) continue;
    const tier = typeof entry === "string" ? undefined : entry.tier;
    const prevIdx = idxBySymbol.get(symbol);
    if (prevIdx != null) {
      const kept = out[prevIdx];
      const keptTier = typeof kept === "string" ? undefined : kept.tier;
      // held > view: only a view-tagged kept entry is upgraded by an
      // incoming held/bare copy (a bare string means held at the relay).
      if (keptTier === "view" && tier !== "view") {
        out[prevIdx] = tier ? { symbol, tier } : symbol;
      }
      continue;
    }
    idxBySymbol.set(symbol, out.length);
    out.push(tier ? { symbol, tier } : symbol);
  }
  return out;
}
