import type { PriceData } from "@/lib/pricesProtocol";
import { parseOptionKey } from "@/lib/pricesProtocol";

/**
 * Is an option past its expiry? Both args are `YYYYMMDD` calendar dates compared
 * as strings (lexicographic order matches chronological order for that format).
 * An option is exercisable THROUGH its expiry date, so it only counts as expired
 * the day AFTER. `today` must be computed in the market timezone (ET) by the
 * caller — a HKT-local "today" would mark a contract expired a day early.
 * Malformed expiries are never treated as expired (don't auto-hide on bad data).
 */
export function isOptionExpired(expiry: string, todayYmd: string): boolean {
  if (!/^\d{8}$/.test(expiry)) return false;
  return todayYmd > expiry;
}

export type BookSubject = {
  bookKind: "stock" | "option";
  bookKey: string;
  quotePriceData: PriceData | null;
};

/**
 * Decide what instrument the cockpit BOOK shows, independent of the held
 * position that drives the ticket/panel. The rule treats the stock and option
 * as distinct URLs:
 *
 * - `?leg=<optionKey>` (explicit) → that option's book.
 * - else a `?posId=`-selected single-leg option position → its option book
 *   (preserves "click your position → see its book" without changing nav).
 * - else (bare `/TICKER`) → the underlying STOCK book.
 *
 * An option past expiry falls back to the stock book (dead instrument). A `leg`
 * whose underlying is not this ticker is ignored. The header/book quote follows
 * the chosen subject: the held-position quote (which carries the calculated-mark
 * fallback) when the option IS the position, else the subject's own WS price.
 */
export function resolveBookSubject(args: {
  ticker: string;
  leg: string | null;
  hasPosId: boolean;
  positionOptionKey: string | null;
  positionPriceData: PriceData | null;
  prices: Record<string, PriceData>;
  todayYmd: string;
}): BookSubject {
  const {
    ticker,
    leg,
    hasPosId,
    positionOptionKey,
    positionPriceData,
    prices,
    todayYmd,
  } = args;

  const candidate = leg ?? (hasPosId ? positionOptionKey : null);
  const parsed = candidate ? parseOptionKey(candidate) : null;
  const matchesTicker = parsed?.symbol?.toUpperCase() === ticker.toUpperCase();
  const expired = parsed ? isOptionExpired(parsed.expiry, todayYmd) : false;

  if (parsed && matchesTicker && !expired) {
    const bookKey = candidate as string;
    const quotePriceData =
      positionOptionKey === bookKey
        ? positionPriceData
        : (prices[bookKey] ?? null);
    return { bookKind: "option", bookKey, quotePriceData };
  }

  return {
    bookKind: "stock",
    bookKey: ticker,
    quotePriceData: prices[ticker] ?? null,
  };
}

/** Today's calendar date in ET as `YYYYMMDD` (option-expiry timezone). */
export function etTodayYmd(now: Date = new Date()): string {
  // en-CA renders YYYY-MM-DD; strip the dashes.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  })
    .format(now)
    .replaceAll("-", "");
}
