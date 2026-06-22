"use client";

import type { PortfolioPosition } from "@/lib/types";
import { resolveEntryCost, resolveMarketValue } from "@/lib/positionUtils";
import { fmtUsd, toneClass } from "@/lib/format";

type ActHeldSummaryProps = {
  position: PortfolioPosition;
  /** Opens the full position detail (legs / cards / close-out) in the p-deck. */
  onOpenDeck: () => void;
};

/**
 * Subtle one-line held-position cue for the act column. NOT the position panel:
 * it carries a single line (structure + P&L) and links to the p-deck, keeping the
 * act column as clean as the flat futures view. Full detail lives in the deck.
 */
export default function ActHeldSummary({
  position,
  onOpenDeck,
}: ActHeldSummaryProps) {
  // Foreign positions carry native (JPY/KRW) entry/market values; use the
  // backend-converted USD siblings so this cue never shows a ¥/₩ magnitude as $.
  // (This cue has no live prices, so the sync-time *_usd snapshot is correct.)
  const cur = (position.currency || "USD").toUpperCase();
  let pnl: number | null;
  if (cur === "USD") {
    const marketValue = resolveMarketValue(position);
    pnl = marketValue != null ? marketValue - resolveEntryCost(position) : null;
  } else {
    pnl =
      position.market_value_usd != null && position.entry_cost_usd != null
        ? position.market_value_usd - position.entry_cost_usd
        : null;
  }

  return (
    <button type="button" className="act-flat" onClick={onOpenDeck}>
      <span>{position.structure}</span>
      <span className="act-flat-hint">
        {pnl != null && (
          <>
            P&amp;L <span className={toneClass(pnl)}>{fmtUsd(pnl)}</span> ·{" "}
          </>
        )}
        open ↑
      </span>
    </button>
  );
}
