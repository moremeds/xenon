"use client";

import type { OrdersData, PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import ChatPanel from "@/components/ChatPanel";
import { DashboardSection } from "./DashboardSection";
import { PortfolioSnapshotCard } from "./PortfolioSnapshotCard";
import { OrdersSnapshotCard } from "./OrdersSnapshotCard";

type Props = {
  portfolio: PortfolioData | null;
  orders: OrdersData | null;
  prices?: Record<string, PriceData>;
};

/**
 * DashboardSurface — the /dashboard landing surface. Two columns:
 *
 *   LEFT  — Portfolio snapshot + Working/Filled orders (collapsible).
 *   RIGHT — AI Assistant (ChatPanel) in a rail; xenon's "live market intel"
 *           surface, the native replacement for radon's news rail.
 *
 * Off-identity radon widgets (Trading Candidates / scanner, Live Market Feed /
 * scraper) are intentionally omitted. Orders is IB-only; under the FUTU tab the
 * shell passes orders={null} and OrdersSnapshotCard shows its empty state.
 */
export default function DashboardSurface({ portfolio, orders, prices }: Props) {
  return (
    <div className="dashboard-surface">
      <div className="dashboard-surface__main">
        <DashboardSection id="portfolio" label="Portfolio" count="01">
          <PortfolioSnapshotCard portfolio={portfolio} prices={prices} />
        </DashboardSection>
        <DashboardSection id="orders" label="Working & Filled" count="02">
          <OrdersSnapshotCard orders={orders} />
        </DashboardSection>
      </div>
      <aside className="dashboard-surface__rail" aria-label="AI Assistant">
        <ChatPanel activeSection="dashboard" />
      </aside>
    </div>
  );
}
