import { useMemo } from "react";
import type { PriceData } from "@/lib/pricesProtocol";
import { usdPerUnitFromForexTick } from "@/lib/fx";

/** Merge live forex-tick-derived USD-per-unit rates over a payload fallback,
 *  for the set of currencies present in the portfolio. USD is always 1. A live
 *  USD.<cur> tick (when present + valid) overrides the boot/sync fallback rate. */
export function useFx(
  prices: Record<string, PriceData>,
  fallback: Record<string, number>,
  currencies: string[],
): Record<string, number> {
  const currencyKey = currencies.join(",");
  return useMemo(() => {
    const out: Record<string, number> = { USD: 1, ...(fallback || {}) };
    for (const cur of currencies) {
      if (cur === "USD") continue;
      const live = usdPerUnitFromForexTick(
        `USD.${cur}`,
        prices[`USD.${cur}`]?.last ?? null,
      );
      if (live) out[live.currency] = live.rate;
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- currencyKey content-keys `currencies`
  }, [prices, fallback, currencyKey]);
}
