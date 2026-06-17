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
import {
  resolveBookSubject,
  etTodayYmd,
  positionBookKey,
} from "@/lib/book/bookSubject";
import { useTickerDetail } from "@/lib/TickerDetailContext";
import { isUrlDeck, legacyTabToDeck, type DeckKey } from "@/lib/deckNav";
import AssetCockpit from "./ticker-detail/AssetCockpit";

export type TickerDetailContentProps = {
  ticker: string;
  positionId?: number | null;
  /** `?leg=<optionKey>` — selects the option book; null/absent = stock book. */
  leg?: string | null;
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
  leg,
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
  const { priceData, isSpreadNet } = useMemo(
    () => resolveTickerQuote(ticker, position, prices),
    [ticker, position, prices],
  );

  // The cockpit BOOK is URL-driven, treating the underlying and each option as
  // distinct subjects: `?leg=<optionKey>` (or a `?posId`-selected single-leg
  // option) shows that option's book; the bare ticker shows the underlying STOCK
  // book — so the book head's underlying link (`/TICKER`) actually reaches the
  // stock. An option past expiry falls back to the stock book. The ticket /
  // position panel stay driven by `position` (unchanged).
  const positionOptionKey = positionBookKey(position);
  const subject = useMemo(
    () =>
      resolveBookSubject({
        ticker,
        leg: leg ?? null,
        hasPosId: positionId != null && positionId >= 0,
        positionOptionKey,
        positionPriceData: priceData,
        prices,
        todayYmd: etTodayYmd(),
      }),
    [ticker, leg, positionId, positionOptionKey, priceData, prices],
  );
  const bookKind: "stock" | "option" | "future" = subject.bookKind;
  const bookKey = subject.bookKey;

  // Header quote follows the book subject. Exception: a multi-leg position keeps
  // its signed spread-net header over the underlying book (legacy behavior — a
  // spread has no single-contract book), so multi-leg viewing is unchanged.
  const isMultiLeg = position != null && position.legs.length > 1;
  const quotePriceData =
    subject.bookKind === "stock" && isMultiLeg
      ? priceData
      : subject.quotePriceData;
  const headerSpreadNet =
    subject.bookKind === "stock" && isMultiLeg ? isSpreadNet : false;

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
      isSpreadNet={headerSpreadNet}
      tickerOrders={tickerOrders}
      depths={depths}
      tape={tape}
      theme={theme}
      activeDeck={activeDeck}
      onDeckChange={onDeckChange}
    />
  );
}
