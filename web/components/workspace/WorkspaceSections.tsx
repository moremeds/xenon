"use client";

import { usePerfTracker } from "@/lib/perfTracker";
import type { OrdersData, PortfolioData, WorkspaceSection } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import PerformancePanel from "../PerformancePanel";
import TickerWorkspace from "../TickerWorkspace";
import { MarketState } from "@/lib/useMarketHours";
import type { FutuInitialRead } from "@/lib/useFutuPortfolio";
import { JournalSections } from "./JournalSections";
import { OrdersSections } from "./OrdersSections";
import { PortfolioSections } from "./PortfolioSections";

/* ─── Root switch ───────────────────────────────────────── */

type WorkspaceSectionsProps = {
  section: WorkspaceSection;
  portfolio?: PortfolioData | null;
  portfolioLastSync?: string | null;
  orders?: OrdersData | null;
  prices?: Record<string, PriceData>;
  tickerParam?: string;
  theme?: "dark" | "light";
  marketState?: MarketState;
  /**
   * Which broker account's data is being rendered. Defaults to "ib" for
   * backward compat with tests that render WorkspaceSections directly
   * without supplying this prop.
   *
   * When "futu": the portfolio PositionTable instances render in readonly
   * mode so clicks cannot reach IB order-placement surfaces. Other
   * sections (blotter, order history) are unaffected — they always show
   * IB data regardless of the broker tab.
   */
  activeAccount?: "ib" | "futu";
  /**
   * Futu tab only: result of the initial /api/futu/portfolio read from
   * useFutuPortfolio(). When it resolves to `never_synced` (404) or
   * `unavailable` (502/other) the portfolio section renders a
   * measurement-condition empty state with a retry action instead of
   * LOADING forever. Absent/`pending` keeps the existing LOADING panel
   * (also used by the IB tab, whose hook has no such state).
   */
  futuRead?: FutuInitialRead;
  /** Futu tab only: retry action wired to useFutuPortfolio().syncNow(). */
  onFutuRetry?: () => void;
  futuRetrying?: boolean;
  /** Futu tab: true when health says trading[env] is on. */
  futuOrderActionsEnabled?: boolean;
  futuAccountIdTail?: string | null;
  futuAccountEnv?: import("@/lib/futuAccountEnv").FutuAccountEnv;
};

// =============================================================================
// UW Analysis — tiered ticker grid + single detail panel
// =============================================================================

function fmtNum(v: number | null | undefined, digits = 2, suffix = ""): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v.toFixed(digits)}${suffix}`;
}

function fmtCompact(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return v.toFixed(0);
}

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

function biasPillClass(bias: string | undefined): string {
  if (!bias) return "pill neutral";
  const b = bias.toUpperCase();
  if (b.startsWith("BULL")) return "pill defined";
  if (b.startsWith("BEAR")) return "pill distrib";
  return "pill neutral";
}

export default function WorkspaceSections({
  section,
  portfolio,
  portfolioLastSync,
  orders,
  prices,
  tickerParam,
  theme,
  marketState,
  activeAccount = "ib",
  futuRead,
  onFutuRetry,
  futuRetrying,
  futuOrderActionsEnabled = false,
  futuAccountIdTail = null,
  futuAccountEnv = "paper",
}: WorkspaceSectionsProps) {
  usePerfTracker("WorkspaceSections");
  switch (section) {
    case "dashboard":
      return null;
    case "portfolio":
      return (
        <PortfolioSections
          portfolio={portfolio ?? null}
          prices={prices}
          activeAccount={activeAccount}
          futuRead={futuRead}
          onFutuRetry={onFutuRetry}
          futuRetrying={futuRetrying}
          futuOrderActionsEnabled={futuOrderActionsEnabled}
          futuAccountIdTail={futuAccountIdTail}
          futuAccountEnv={futuAccountEnv}
        />
      );
    case "performance":
      return (
        <PerformancePanel
          portfolioLastSync={portfolioLastSync}
          marketState={marketState}
          broker={activeAccount === "futu" ? "FUTU" : "IB"}
        />
      );
    case "orders":
      return (
        <OrdersSections
          orders={orders ?? null}
          prices={prices}
          portfolio={portfolio}
          activeAccount={activeAccount}
          futuOrderActionsEnabled={futuOrderActionsEnabled}
        />
      );
    case "journal":
      return <JournalSections activeAccount={activeAccount} />;
    case "ticker-detail":
      return tickerParam ? (
        <TickerWorkspace ticker={tickerParam} theme={theme ?? "dark"} />
      ) : null;
    default:
      return (
        <PortfolioSections
          portfolio={portfolio ?? null}
          prices={prices}
          activeAccount={activeAccount}
          futuRead={futuRead}
          onFutuRetry={onFutuRetry}
          futuRetrying={futuRetrying}
          futuOrderActionsEnabled={futuOrderActionsEnabled}
          futuAccountIdTail={futuAccountIdTail}
          futuAccountEnv={futuAccountEnv}
        />
      );
  }
}
