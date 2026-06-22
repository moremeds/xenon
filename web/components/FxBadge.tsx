"use client";

/** Live FX badge: one capsule per non-USD currency, shown as the human pair
 *  USD/XXX (= 1 / usd_per_unit). Filled dot = live IDEALPRO tick for THAT pair,
 *  hollow = snapshot/fallback rate. Liveness is per-currency, not global. */
export default function FxBadge({
  rates,
  liveCurrencies = [],
  inline = false,
}: {
  rates: Record<string, number>;
  liveCurrencies?: string[];
  /** Inline header placement (e.g. after a ticker name) — drops the
   *  standalone-row bottom margin so the capsule sits vertically centered. */
  inline?: boolean;
}) {
  const liveSet = new Set(liveCurrencies.map((c) => c.toUpperCase()));
  const pairs = Object.entries(rates)
    .filter(([cur, rate]) => cur !== "USD" && rate > 0)
    .map(([cur, rate]) => ({
      cur,
      perUsd: 1 / rate,
      live: liveSet.has(cur.toUpperCase()),
    }));
  if (pairs.length === 0) return null;
  return (
    <span
      className={
        inline ? "fx-badge-group fx-badge-group-inline" : "fx-badge-group"
      }
    >
      {pairs.map(({ cur, perUsd, live }) => (
        <span
          key={cur}
          className="fx-badge"
          title={live ? "Live IDEALPRO" : "Snapshot rate"}
        >
          <span
            className={live ? "fx-dot fx-dot-live" : "fx-dot"}
            aria-hidden
          />
          {`USD/${cur} ${perUsd.toLocaleString("en-US", {
            maximumFractionDigits: cur === "KRW" ? 1 : 2,
          })}`}
        </span>
      ))}
    </span>
  );
}
