"use client";

import { useTickerNav } from "@/lib/useTickerNav";

export default function TickerLink({ ticker, positionId }: { ticker: string; positionId?: number }) {
  const { navigateToTicker } = useTickerNav();
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
