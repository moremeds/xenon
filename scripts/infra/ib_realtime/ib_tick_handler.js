/**
 * IB tick handler — pure functions for processing tickPrice/tickSize events.
 * Extracted from ib_realtime_server.js so they can be unit-tested independently.
 */

import { IBApiTickType as TICK_TYPE } from "@stoqey/ib";

export function normalizeNumber(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return null;
  }
  return value;
}

export function createPriceData(symbol) {
  return {
    symbol,
    last: null,
    lastIsCalculated: false,
    bid: null,
    ask: null,
    bidSize: null,
    askSize: null,
    volume: null,
    high: null,
    low: null,
    open: null,
    close: null,
    // Misc Stats (generic tick 165)
    week52High: null,
    week52Low: null,
    avgVolume: null,
    delta: null,
    gamma: null,
    theta: null,
    vega: null,
    impliedVol: null,
    undPrice: null,
    timestamp: new Date().toISOString(),
  };
}

// Cash indexes report value via CLOSE tick, not LAST. Stocks should NOT
// fall back to CLOSE — IB's close is the PREVIOUS session's close and can
// be days stale on weekends, giving wildly inaccurate "underlying" prices.
const CASH_INDEX_SYMBOLS = new Set([
  "VIX",
  "VVIX",
  "COR1M",
  "SPX",
  "NDX",
  "RUT",
  "DJX",
  "OVX",
  "MOVE",
]);

export function updateDerivedLast(data) {
  if (data.last == null && data.bid != null && data.ask != null) {
    const midpoint = (data.bid + data.ask) / 2;
    data.last = Number.isFinite(midpoint) ? Number(midpoint.toFixed(4)) : null;
    data.lastIsCalculated = true;
  }
  // Only fall back to close for cash indexes — their value IS the close tick.
  // For stocks/options, leave last as null so the UI shows "---" rather than
  // a stale previous-session close masquerading as a current price.
  if (data.last == null && data.close != null) {
    const baseSymbol = data.symbol.split("_")[0];
    if (CASH_INDEX_SYMBOLS.has(baseSymbol)) {
      data.last = data.close;
      data.lastIsCalculated = true;
    }
  }
}

// IB serves market data on two independent planes: the streaming `reqMktData`
// subscription and the `reqHistoricalData` bars. A streaming line can FREEZE
// per-contract — if it was starved (error 101 "max tickers") across a session
// boundary it never advances, and keeps emitting the last session's CLOSE/LAST
// it managed to receive (e.g. TSLA stuck at Jul-1 425.30/423.60 while the real
// Jul-2 close is 393.45). The historical plane is pulled fresh each call, so
// the most recent daily bar's close is authoritative.
//
// When there is no live trade (line stale, or `last` absent), the last known
// price IS that daily close — so override the frozen streaming value with it.
// A genuine live `last` (not calculated, not stale) is left untouched; only its
// `close` is corrected. Options are skipped — they use optionCloseCache.
// Mutates `data` in place; returns true iff it changed `last` or `close`.
export function applyAuthoritativeClose(data, dailyClose, stale) {
  if (typeof dailyClose !== "number" || !(dailyClose > 0)) return false;
  if (data.symbol.includes("_")) return false; // options: separate close cache
  let changed = false;
  if (data.close !== dailyClose) {
    data.close = dailyClose;
    changed = true;
  }
  // No live trade to trust: last is missing, was derived, or the line is stale.
  const noLiveTrade = data.last == null || data.lastIsCalculated || stale;
  if (noLiveTrade && data.last !== dailyClose) {
    data.last = dailyClose;
    data.lastIsCalculated = true; // a session close, not a live trade print
    changed = true;
  }
  return changed;
}

// Pick the close of the most-recent COMPLETED daily session from historical
// bars. An in-progress current-day bar (returned by "2 D"/"1 day" during RTH)
// is rejected, so a partial intraday value is never cached and served as a
// session close. `bars` = [{ date: "YYYYMMDD", close }] oldest→newest (IB order);
// `todayYmd` = "YYYYMMDD" in ET; `sessionOpen` = is today's RTH session in
// progress right now. Returns { close, date } of the latest completed session,
// or null when none qualifies.
export function selectCompletedBarClose(bars, todayYmd, sessionOpen) {
  let best = null;
  for (const b of bars) {
    if (typeof b.close !== "number" || !(b.close > 0)) continue;
    const isTodayPartial = b.date === todayYmd && sessionOpen;
    if (isTodayPartial) continue; // today's bar is still forming — not a close
    best = { close: b.close, date: b.date }; // newer completed bar wins
  }
  return best;
}

// Fields whose change means the symbol produced a real, fresh update. Closed-
// market -1 quotes normalize to null → no change → timestamp is NOT bumped, so
// a stale `last` carries an honest (old) timestamp instead of a lying fresh one.
const FRESHNESS_FIELDS = [
  "last",
  "bid",
  "ask",
  "close",
  "volume",
  "high",
  "low",
  "open",
];

function snapshotFreshness(data) {
  const snap = {};
  for (const f of FRESHNESS_FIELDS) snap[f] = data[f];
  return snap;
}

function freshnessChanged(prev, data) {
  for (const f of FRESHNESS_FIELDS) {
    if (prev[f] !== data[f]) return true;
  }
  return false;
}

// Returns true iff this tick changed a freshness field (and bumped timestamp).
export function updatePriceFromTickPrice(data, tickType, value) {
  const prev = snapshotFreshness(data);
  switch (tickType) {
    // ── Live tick types ───────────────────────────────────────────────────
    case TICK_TYPE.BID:
      data.bid = normalizeNumber(value);
      data.lastIsCalculated = false;
      break;
    case TICK_TYPE.ASK:
      data.ask = normalizeNumber(value);
      data.lastIsCalculated = false;
      break;
    case TICK_TYPE.LAST:
      data.last = normalizeNumber(value);
      data.lastIsCalculated = false;
      break;
    case TICK_TYPE.HIGH:
      data.high = normalizeNumber(value);
      break;
    case TICK_TYPE.LOW:
      data.low = normalizeNumber(value);
      break;
    case TICK_TYPE.OPEN:
      data.open = normalizeNumber(value);
      break;
    case TICK_TYPE.CLOSE:
      data.close = normalizeNumber(value);
      break;
    case TICK_TYPE.VOLUME:
      data.volume = normalizeNumber(value);
      break;

    // ── Misc Stats (generic tick 165) ─────────────────────────────────────
    case TICK_TYPE.LOW_52_WEEK: // 19
      data.week52Low = normalizeNumber(value);
      break;
    case TICK_TYPE.HIGH_52_WEEK: // 20
      data.week52High = normalizeNumber(value);
      break;
    case TICK_TYPE.AVG_VOLUME: // 21
      data.avgVolume = normalizeNumber(value);
      break;

    // ── Delayed tick types (reqMarketDataType(4) fallback for indexes) ────
    // IB sends these instead of live types when a real-time subscription is
    // absent. VIX/VVIX always receive delayed ticks because they require a
    // separate CBOE index subscription.
    case TICK_TYPE.DELAYED_BID: // 66
      data.bid = normalizeNumber(value);
      data.lastIsCalculated = false;
      break;
    case TICK_TYPE.DELAYED_ASK: // 67
      data.ask = normalizeNumber(value);
      data.lastIsCalculated = false;
      break;
    case TICK_TYPE.DELAYED_LAST: // 68
      data.last = normalizeNumber(value);
      data.lastIsCalculated = false;
      break;
    case TICK_TYPE.DELAYED_HIGH: // 72
      data.high = normalizeNumber(value);
      break;
    case TICK_TYPE.DELAYED_LOW: // 73
      data.low = normalizeNumber(value);
      break;
    case TICK_TYPE.DELAYED_VOLUME: // 74
      data.volume = normalizeNumber(value);
      break;
    case TICK_TYPE.DELAYED_CLOSE: // 75
      data.close = normalizeNumber(value);
      break;
    case TICK_TYPE.DELAYED_OPEN: // 76
      data.open = normalizeNumber(value);
      break;

    default:
      break;
  }

  if (data.last == null) {
    updateDerivedLast(data);
  }

  // ── Stale frozen LAST detection for options ──
  // IB with reqMarketDataType(4) sends frozen LAST = yesterday's close before
  // live ticks arrive. For options (symbol contains "_"), if LAST equals CLOSE
  // and bid/ask indicate a very different price (>20% divergence), replace LAST
  // with bid/ask midpoint. Stocks are excluded — last=close is normal after hours.
  if (
    data.last != null &&
    data.close != null &&
    data.last === data.close &&
    data.bid != null &&
    data.ask != null &&
    data.symbol.includes("_") // options only (keyed as SYMBOL_EXPIRY_STRIKE_RIGHT)
  ) {
    const mid = (data.bid + data.ask) / 2;
    const divergence = Math.abs(mid - data.last) / data.last;
    if (divergence > 0.2) {
      data.last = Number(mid.toFixed(4));
      data.lastIsCalculated = true;
    }
  }

  const changed = freshnessChanged(prev, data);
  if (changed) {
    data.timestamp = new Date().toISOString();
  }
  return changed;
}

/* ─── Fundamentals (tickString type 47) ─────────────────────── */

/**
 * IB sentinel for "no value" — DBL_MAX or values > 1e300.
 */
function isSentinel(v) {
  return !Number.isFinite(v) || Math.abs(v) > 1e300;
}

export function createFundamentalsData(symbol) {
  return {
    symbol,
    peRatio: null,
    eps: null,
    dividendYield: null,
    week52High: null,
    week52Low: null,
    priceBookRatio: null,
    roe: null,
    revenue: null,
    timestamp: new Date().toISOString(),
  };
}

/**
 * IB fundamental ratios arrive as semicolon-delimited key=value pairs:
 *   "PEEXCLXOR=25.3;YIELD=1.5;NHIG=185.0;NLOW=120.5;..."
 *
 * Known keys:
 *   PEEXCLXOR  — P/E excluding extraordinary items
 *   TTMEPSXCLX — Trailing 12m EPS excl extra
 *   YIELD      — Dividend yield (%)
 *   NHIG       — 52-week high
 *   NLOW       — 52-week low
 *   MKTCAP     — Market cap (millions)
 *   PRICE2BK   — Price/book ratio
 *   TTMROEPCT  — Trailing 12m ROE (%)
 *   TTMREV     — Trailing 12m revenue
 */
const FUNDAMENTAL_FIELD_MAP = {
  PEEXCLXOR: "peRatio",
  TTMEPSXCLX: "eps",
  YIELD: "dividendYield",
  NHIG: "week52High",
  NLOW: "week52Low",
  PRICE2BK: "priceBookRatio",
  TTMROEPCT: "roe",
  TTMREV: "revenue",
};

export function parseFundamentalRatios(data, fundString) {
  if (typeof fundString !== "string" || fundString.length === 0) return false;

  const pairs = fundString.split(";");
  let updated = false;

  for (const pair of pairs) {
    const eqIdx = pair.indexOf("=");
    if (eqIdx < 1) continue;
    const key = pair.substring(0, eqIdx).trim();
    const field = FUNDAMENTAL_FIELD_MAP[key];
    if (!field) continue;

    const val = parseFloat(pair.substring(eqIdx + 1));
    if (isSentinel(val)) continue;

    data[field] = val;
    updated = true;
  }

  if (updated) {
    data.timestamp = new Date().toISOString();
  }
  return updated;
}

// Returns true iff this size tick changed a freshness field (and bumped timestamp).
export function updatePriceFromTickSize(data, sizeType, value) {
  const prev = snapshotFreshness(data);
  switch (sizeType) {
    case TICK_TYPE.BID_SIZE:
      data.bidSize = normalizeNumber(value);
      break;
    case TICK_TYPE.ASK_SIZE:
      data.askSize = normalizeNumber(value);
      break;
    case TICK_TYPE.VOLUME:
      data.volume = normalizeNumber(value);
      break;
    case TICK_TYPE.DELAYED_BID_SIZE: // 69
      data.bidSize = normalizeNumber(value);
      break;
    case TICK_TYPE.DELAYED_ASK_SIZE: // 70
      data.askSize = normalizeNumber(value);
      break;
    case TICK_TYPE.DELAYED_VOLUME: // 74
      data.volume = normalizeNumber(value);
      break;
    case TICK_TYPE.LAST_SIZE:
      break;
    default:
      break;
  }

  const changed = freshnessChanged(prev, data);
  if (changed) {
    data.timestamp = new Date().toISOString();
  }
  return changed;
}
