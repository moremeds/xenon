"use client";

import type { OrdersData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import ChatPanel from "@/components/ChatPanel";
import { DashboardSection } from "./DashboardSection";
import {
  PortfolioSnapshotCard,
  type DashboardAccount,
} from "./PortfolioSnapshotCard";
import { OrdersSnapshotCard } from "./OrdersSnapshotCard";

type Props = {
  accounts: DashboardAccount[];
  orders: OrdersData | null;
  prices?: Record<string, PriceData>;
};

/**
 * DashboardSurface — the /dashboard landing surface.
 *
 *   TOP STRIP — Portfolio (merged IB+FUTU, click-to-break-down) and
 *               Working/Filled orders side-by-side (collapsible).
 *   BELOW     — AI Assistant (ChatPanel) full-width; xenon's primary surface.
 *
 * Off-identity radon widgets (Trading Candidates, Live Market Feed) are omitted.
 * Working & Filled is IB-only (Futu has no orders).
 */
export default function DashboardSurface({ accounts, orders, prices }: Props) {
  return (
    <div className="dashboard-surface">
      <div className="dashboard-surface__strip">
        <DashboardSection id="portfolio" label="Portfolio" count="01">
          <PortfolioSnapshotCard accounts={accounts} prices={prices} />
        </DashboardSection>
        <DashboardSection id="orders" label="Working & Filled" count="02">
          <OrdersSnapshotCard orders={orders} />
        </DashboardSection>
      </div>
      <div className="dashboard-surface__chat">
        <ChatPanel activeSection="dashboard" />
      </div>
    </div>
  );
}
