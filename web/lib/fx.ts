/** Native-currency → USD conversion (mirror of src/xenon/utils/fx.py).
 *  usdPerUnit[cur] = USD value of 1 unit of `cur`, so usd = native * rate. */
export function toUsd(
  amount: number | null,
  currency: string | null,
  usdPerUnit: Record<string, number>,
): number | null {
  if (amount == null || !Number.isFinite(amount)) return null;
  const cur = (currency || "USD").toUpperCase();
  if (cur === "USD") return amount;
  const rate = usdPerUnit[cur];
  if (rate == null || !(rate > 0)) return null;
  return amount * rate;
}

/** Forex tick "USD.JPY"=161.5 is JPY-per-USD → invert to USD-per-JPY. Only
 *  USD-base pairs are usable for converting a native amount to USD here. */
export function usdPerUnitFromForexTick(
  pairKey: string,
  last: number | null,
): { currency: string; rate: number } | null {
  const [base, quote] = pairKey.split(".");
  if (base !== "USD" || !quote) return null;
  if (last == null || !(last > 0)) return null;
  return { currency: quote, rate: 1 / last };
}

const ZERO_DECIMAL = new Set(["JPY", "KRW"]);

/** Format a native amount with its currency symbol. JPY/KRW carry no minor
 *  unit, so 0 fraction digits; everything else 2. Returns "---" for null. */
export function fmtNative(amount: number | null, currency: string): string {
  if (amount == null || !Number.isFinite(amount)) return "---";
  const cur = (currency || "USD").toUpperCase();
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: cur,
      maximumFractionDigits: ZERO_DECIMAL.has(cur) ? 0 : 2,
    }).format(amount);
  } catch {
    return `${amount.toLocaleString("en-US")} ${cur}`;
  }
}
