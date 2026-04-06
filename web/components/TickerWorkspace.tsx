"use client";

import { useCallback } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { useTickerDetail } from "@/lib/TickerDetailContext";
import TickerDetailContent from "./TickerDetailContent";

const VALID_TABS = new Set(["company", "book", "chain", "position", "order", "news", "ratings", "seasonality"]);

type TickerWorkspaceProps = {
  ticker: string;
  theme: "dark" | "light";
};

export default function TickerWorkspace({ ticker, theme }: TickerWorkspaceProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { getPrices, getFundamentals, getPortfolio, getOrders } = useTickerDetail();

  const prices = getPrices();
  const fundamentals = getFundamentals();
  const portfolio = getPortfolio();
  const orders = getOrders();

  // Read tab from URL, validate, default to "company"
  const rawTab = searchParams.get("tab");
  const activeTab = rawTab && VALID_TABS.has(rawTab) ? rawTab : "company";
  const positionId = searchParams.get("posId") ? Number(searchParams.get("posId")) : null;

  // Tab change → router.replace (no history pollution)
  const setTab = useCallback((tab: string) => {
    const params = new URLSearchParams(searchParams.toString());
    if (tab === "company") {
      params.delete("tab");
    } else {
      params.set("tab", tab);
    }
    const qs = params.toString();
    router.replace(`/${ticker}${qs ? `?${qs}` : ""}`, { scroll: false });
  }, [router, ticker, searchParams]);

  return (
    <div className="ticker-detail-page">
      <button className="ticker-back-nav" onClick={() => router.back()}>
        <ArrowLeft size={14} /> Back
      </button>

      <TickerDetailContent
        ticker={ticker}
        positionId={positionId}
        activeTab={activeTab}
        onTabChange={setTab}
        prices={prices}
        fundamentals={fundamentals}
        portfolio={portfolio}
        orders={orders}
        theme={theme}
      />
    </div>
  );
}
