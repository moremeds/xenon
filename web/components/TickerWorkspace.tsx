"use client";

import { useCallback } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { useTickerDetail } from "@/lib/TickerDetailContext";
import { isUrlDeck, legacyTabToDeck } from "@/lib/deckNav";
import TickerDetailContent from "./TickerDetailContent";

type TickerWorkspaceProps = {
  ticker: string;
  theme: "dark" | "light";
};

export default function TickerWorkspace({
  ticker,
  theme,
}: TickerWorkspaceProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const {
    getPrices,
    getFundamentals,
    getPortfolio,
    getOrders,
    getDepths,
    getTape,
  } = useTickerDetail();

  const prices = getPrices();
  const fundamentals = getFundamentals();
  const portfolio = getPortfolio();
  const orders = getOrders();
  const depths = getDepths();
  const tape = getTape();

  // Deck model: `?deck=<c|p|n|r|s|i>` opens a reference deck; no deck = book-first
  // landing. Legacy `?tab=` values are mapped through legacyTabToDeck so old
  // links/bookmarks still resolve. The resolved deck key is fed through the
  // existing `activeTab` prop ("" when no deck is open).
  const rawDeck = searchParams.get("deck");
  const deck = isUrlDeck(rawDeck)
    ? rawDeck
    : legacyTabToDeck(searchParams.get("tab"));
  const positionId = searchParams.get("posId")
    ? Number(searchParams.get("posId"))
    : null;
  // `?leg=<optionKey>` selects the option book for that contract; absent = the
  // underlying stock book (the bare ticker URL). Kept distinct from `posId` so
  // the book head's underlying link can drop it to reach the stock book.
  const leg = searchParams.get("leg");

  // Deck change → router.replace (no history pollution). Writes `?deck=` when a
  // deck is open, deletes it otherwise. Always drops the legacy `tab` param so
  // `deck` is the single source of truth. Preserves `posId`.
  const setDeck = useCallback(
    (value: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.delete("tab");
      if (isUrlDeck(value)) {
        params.set("deck", value);
      } else {
        params.delete("deck");
      }
      const qs = params.toString();
      router.replace(`/${ticker}${qs ? `?${qs}` : ""}`, { scroll: false });
    },
    [router, ticker, searchParams],
  );

  return (
    <div className="ticker-detail-page">
      <button className="ticker-back-nav" onClick={() => router.back()}>
        <ArrowLeft size={14} /> Back
      </button>

      <TickerDetailContent
        ticker={ticker}
        positionId={positionId}
        leg={leg}
        activeTab={deck ?? ""}
        onTabChange={setDeck}
        prices={prices}
        fundamentals={fundamentals}
        portfolio={portfolio}
        orders={orders}
        depths={depths}
        tape={tape}
        theme={theme}
      />
    </div>
  );
}
