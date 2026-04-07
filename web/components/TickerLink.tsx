"use client";

import { useTickerNav } from "@/lib/useTickerNav";

/**
 * Renders a ticker symbol. When `disabled=true` (e.g. inside a read-only
 * Futu-tab PositionTable), renders as a non-interactive `<span>` with
 * `aria-disabled="true"` — preserves keyboard tab-order (unlike a disabled
 * button which drops out of focus entirely) while providing zero navigation
 * path to IB-scoped order surfaces.
 *
 * Load-bearing safety control (tribunal T7): disabled=true is the sole
 * guarantee that clicking a Futu position's ticker cell cannot reach
 * `/api/orders/place`.
 */
export default function TickerLink({
  ticker,
  positionId,
  disabled = false,
}: {
  ticker: string;
  positionId?: number;
  disabled?: boolean;
}) {
  const { navigateToTicker } = useTickerNav();

  if (disabled) {
    return (
      <span
        className="ticker-link ticker-link-disabled"
        aria-disabled="true"
        aria-label={`${ticker} — read-only (Futu account)`}
      >
        {ticker}
      </span>
    );
  }

  return (
    <button
      className="ticker-link"
      onClick={() => navigateToTicker(ticker, positionId)}
      aria-label={`View details for ${ticker}`}
    >
      {ticker}
    </button>
  );
}
