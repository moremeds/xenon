"use client";

import { useEffect, useMemo, useState } from "react";
import type {
  OpenOrder,
  PortfolioPosition,
  PortfolioData,
  OrdersData,
} from "@/lib/types";
import type {
  PriceData,
  FundamentalsData,
  DepthBook,
  Trade,
} from "@/lib/pricesProtocol";
import { resolveTickerQuote } from "@/lib/tickerQuote";
import { useTickerDetail } from "@/lib/TickerDetailContext";
import { isUrlDeck, legacyTabToDeck, type DeckKey } from "@/lib/deckNav";
import AssetCockpit from "./ticker-detail/AssetCockpit";

export type TickerDetailContentProps = {
  ticker: string;
  positionId?: number | null;
  activeTab: string;
  onTabChange: (tab: string) => void;
  prices: Record<string, PriceData>;
  fundamentals: Record<string, FundamentalsData>;
  portfolio: PortfolioData | null;
  orders: OrdersData | null;
  depths: Record<string, DepthBook>;
  tape: Record<string, Trade[]>;
  theme: "dark" | "light";
};

export default function TickerDetailContent({
  ticker,
  positionId,
  activeTab,
  onTabChange,
  prices,
  fundamentals,
  portfolio,
  orders,
  depths,
  tape,
  theme,
}: TickerDetailContentProps) {
  const { setFocusedBookKey } = useTickerDetail();
  const position: PortfolioPosition | null = useMemo(() => {
    if (!portfolio) return null;
    // Synthetic ids (negative) come from the fused virtual-pair path in
    // buildTickerGroups — they don't exist in portfolio.positions, so skip
    // the id-exact search and fall through to the ticker match.
    if (positionId != null && positionId >= 0) {
      const exact = portfolio.positions.find((p) => p.id === positionId);
      if (exact) return exact;
    }
    return portfolio.positions.find((p) => p.ticker === ticker) ?? null;
  }, [ticker, positionId, portfolio]);

  const tickerOrders: OpenOrder[] = useMemo(() => {
    if (!orders) return [];
    return orders.open_orders.filter((o) => o.contract.symbol === ticker);
  }, [ticker, orders]);

  // Single source for the cockpit header / book / ticket. Carries the spread-net
  // flag so combos render a signed net instead of a percent move.
  const {
    priceData,
    priceKey: chartPriceKey,
    isSpreadNet,
  } = useMemo(
    () => resolveTickerQuote(ticker, position, prices),
    [ticker, position, prices],
  );

  // Phase 2 has no L2 depth feed, so the header quote IS the resolved price (the
  // depth-NBBO correction is deferred to Phase 3). bookKey is the single-leg
  // option key when present, else the ticker.
  const quotePriceData = priceData;
  const bookKey = chartPriceKey ?? ticker;

  // Instrument kind for the book/header. A single-leg non-stock position is an
  // option; everything else (stock, index, multi-leg, flat) is a stock book.
  // xenon has no futures path (deferred), so there is no "future" branch here.
  const bookKind: "stock" | "option" | "future" =
    position &&
    position.structure_type !== "Stock" &&
    position.legs.length === 1
      ? "option"
      : "stock";

  // Publish the focused book key upward so WorkspaceShell (the usePrices
  // callsite) can stream L2 depth + tape for exactly this subject. Cleared on
  // unmount so leaving the ticker page releases the scarce depth ticket.
  useEffect(() => {
    setFocusedBookKey(bookKey);
    return () => setFocusedBookKey(null);
  }, [bookKey, setFocusedBookKey]);

  // Deck arbitration. xenon's TickerWorkspace passes the deck key through
  // `activeTab` (it may be a deck key like "c", a legacy tab-name like "chain",
  // or "" for the bare book). URL-addressable decks live in the URL; the
  // local-only command palette (":") and mobile order ticket ("o") live in
  // component state so they never serialize to the URL.
  const urlDeck: DeckKey | null = isUrlDeck(activeTab)
    ? activeTab
    : legacyTabToDeck(activeTab);
  const [localDeck, setLocalDeck] = useState<DeckKey | null>(null);
  const activeDeck: DeckKey | null = urlDeck ?? localDeck;

  const onDeckChange = (deck: DeckKey | null) => {
    // URL-addressable decks (and close → null) flow through onTabChange so
    // TickerWorkspace writes/clears `?deck=`; clear any local-only deck.
    if (deck == null || isUrlDeck(deck)) {
      onTabChange(deck ?? "company");
      setLocalDeck(null);
      return;
    }
    // Local-only decks (":" / "o") have no URL form — drive them from state.
    setLocalDeck(deck);
  };

  // The cockpit is the single ticker-detail layout on every viewport. It adapts
  // internally: desktop = book + act column + glyph rail; mobile = book-first
  // with a horizontal glyph strip and full-screen decks (see AssetCockpit).
  return (
    <AssetCockpit
      ticker={ticker}
      position={position}
      prices={prices}
      fundamentals={fundamentals}
      portfolio={portfolio}
      bookKey={bookKey}
      bookKind={bookKind}
      quotePriceData={quotePriceData}
      priceData={priceData}
      isSpreadNet={isSpreadNet}
      tickerOrders={tickerOrders}
      depths={depths}
      tape={tape}
      theme={theme}
      activeDeck={activeDeck}
      onDeckChange={onDeckChange}
    />
  );
}
