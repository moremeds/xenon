"use client";

import { useTickerNav } from "@/lib/useTickerNav";

/**
 * Renders a ticker symbol. Read-only contexts may still navigate to the
 * ticker workspace; execution surfaces are gated deeper in the order/modal
 * flows rather than by suppressing navigation here.
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

  return (
    <button
      className="ticker-link"
      onClick={() => navigateToTicker(ticker, positionId)}
      aria-label={
        disabled
          ? `View read-only details for ${ticker}`
          : `View details for ${ticker}`
      }
    >
      {ticker}
    </button>
  );
}
