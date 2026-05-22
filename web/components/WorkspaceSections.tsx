"use client";

import { usePerfTracker } from "@/lib/perfTracker";
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Bell,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Circle,
  ClipboardList,
  ArrowDown,
  ArrowUp,
  Loader2,
  Search,
  Sparkles,
  TrendingDown,
  TriangleAlert,
  Wrench,
  XCircle,
} from "lucide-react";
import type {
  BlotterTrade,
  ExecutedOrder,
  OpenOrder,
  OrdersData,
  PortfolioData,
  PortfolioPosition,
  TradeEntry,
  WorkspaceSection,
} from "@/lib/types";
import { useOrderActions } from "@/lib/OrderActionsContext";
import type { PriceData } from "@/lib/pricesProtocol";
import { optionKey } from "@/lib/pricesProtocol";
import { useJournal } from "@/lib/useJournal";
import { MetricCard, SourceBadge } from "@/components/ui/MetricCard";
import { SourcePill } from "./SourcePill";
import { useBlotter } from "@/lib/useBlotter";
import { useSort, type SortDirection } from "@/lib/useSort";
import { useTableFilter } from "@/lib/useTableFilter";
import TableSearch from "./TableSearch";
import PortfolioByStructure from "./PortfolioByStructure";
import { fmtPrice, fmtUsd, legPriceKey } from "@/lib/positionUtils";
import {
  buildOpenOrderDisplayRows,
  type OpenOrderDisplayRow,
  buildExecutedGroupDescription,
  resolveOpenOrderComboPrice,
} from "@/lib/openOrderCombos";
import { buildGroupedComboModifyTarget } from "@/lib/openOrderComboModify";
import PositionTable from "./PositionTable";
import { TableSkeleton } from "@/components/ui/Skeleton";
import CancelOrderDialog from "./CancelOrderDialog";
import ModifyOrderModal from "./ModifyOrderModal";
import type { ModifyOrderRequest } from "@/lib/orderModify";
import PerformancePanel from "./PerformancePanel";
import InfoTooltip from "./InfoTooltip";
import { SECTION_TOOLTIPS } from "@/lib/sectionTooltips";
import TickerLink from "./TickerLink";
import TickerWorkspace from "./TickerWorkspace";
import { MarketState } from "@/lib/useMarketHours";

/* ─── Re-exports for backward compat ──────────────────── */

export {
  fmtUsd,
  fmtPrice,
  fmtPriceOrCalculated,
  resolveMarketValue,
  resolveEntryCost,
  getAvgEntry,
  getMultiplier,
  getLastPriceIsCalculated,
  legPriceKey,
  getOptionDailyChg,
  getLastPrice,
} from "@/lib/positionUtils";

/* ─── Share P&L helpers ────────────────────────────────── */

/** Build a human-readable description from an ExecutedOrder.
 *  e.g. "Long AAOI 2026-04-17 Call $45.00" */
/** Build a human-readable description from an ExecutedOrder.
 *  When realizedPNL is present (closing trade), show the ORIGINAL position
 *  direction: BOT closing = was Short, SLD closing = was Long.
 *  When no realizedPNL (opening trade): BOT = Long, SLD = Short. */
function execOrderDescription(e: ExecutedOrder): string {
  const c = e.contract;
  const isClosing = e.realizedPNL != null;
  const side =
    e.side === "BOT"
      ? isClosing
        ? "Short"
        : "Long"
      : e.side === "SLD"
        ? isClosing
          ? "Long"
          : "Short"
        : e.side;
  if (c.secType === "OPT" && c.strike != null && c.right && c.expiry) {
    const right =
      c.right === "C" || c.right === "CALL"
        ? "Call"
        : c.right === "P" || c.right === "PUT"
          ? "Put"
          : c.right;
    return `${side} ${c.symbol} ${c.expiry} ${right} $${c.strike.toFixed(2)}`;
  }
  return `${side} ${c.symbol}`;
}

function executedOptionContractKey(fill: ExecutedOrder): string | null {
  if (fill.contract.secType !== "OPT") return null;
  if (fill.contract.conId != null) return `conid:${fill.contract.conId}`;

  const symbol = fill.contract.symbol?.toUpperCase();
  const expiry = fill.contract.expiry?.replace(/-/g, "");
  const strike = fill.contract.strike;
  const rightRaw = fill.contract.right;
  const right = rightRaw === "CALL" ? "C" : rightRaw === "PUT" ? "P" : rightRaw;

  if (!symbol || !expiry || !right || strike == null) return null;
  return `${symbol}|${expiry}|${right}|${strike}`;
}

function resolveOpeningLegBasis(
  group: PositionFillGroup,
  allGroups?: PositionFillGroup[],
): {
  entryPrice: number | null;
  entryNotional: number;
  entryTime: string | null;
} {
  if (!allGroups)
    return { entryPrice: null, entryNotional: 0, entryTime: null };

  const closeOptFills = group.fills.filter(
    (fill) => fill.contract.secType === "OPT",
  );
  if (closeOptFills.length === 0)
    return { entryPrice: null, entryNotional: 0, entryTime: null };

  const requiredByContract = new Map<string, number>();
  for (const fill of closeOptFills) {
    const key = executedOptionContractKey(fill);
    if (!key) continue;
    requiredByContract.set(
      key,
      (requiredByContract.get(key) ?? 0) + Math.abs(fill.quantity),
    );
  }
  if (requiredByContract.size === 0)
    return { entryPrice: null, entryNotional: 0, entryTime: null };

  const closeTime = Date.parse(group.time);
  const candidateOpenFills = allGroups
    .filter(
      (candidateGroup) =>
        !candidateGroup.isClosing && candidateGroup.symbol === group.symbol,
    )
    .flatMap((candidateGroup) =>
      candidateGroup.fills.filter((fill) => fill.contract.secType === "OPT"),
    )
    .filter((fill) => {
      const key = executedOptionContractKey(fill);
      if (!key || !requiredByContract.has(key)) return false;
      const openTime = Date.parse(fill.time);
      if (
        !Number.isNaN(closeTime) &&
        !Number.isNaN(openTime) &&
        openTime > closeTime
      )
        return false;
      return true;
    })
    .sort((a, b) => {
      const aTime = Date.parse(a.time);
      const bTime = Date.parse(b.time);
      if (Number.isNaN(aTime) && Number.isNaN(bTime)) return 0;
      if (Number.isNaN(aTime)) return 1;
      if (Number.isNaN(bTime)) return -1;
      return bTime - aTime;
    });

  const remainingByContract = new Map(requiredByContract);
  let matchedQty = 0;
  let netCash = 0;
  let earliestEntryTime: string | null = null;

  for (const fill of candidateOpenFills) {
    const key = executedOptionContractKey(fill);
    if (!key) continue;

    const remainingQty = remainingByContract.get(key) ?? 0;
    if (remainingQty <= 0) continue;
    if (fill.avgPrice == null || !Number.isFinite(fill.avgPrice)) continue;

    const takeQty = Math.min(remainingQty, Math.abs(fill.quantity));
    if (takeQty <= 0) continue;

    const cashSign =
      fill.side === "SLD" || fill.side === "SELL"
        ? 1
        : fill.side === "BOT" || fill.side === "BUY"
          ? -1
          : 0;
    if (cashSign === 0) continue;

    netCash += cashSign * fill.avgPrice * takeQty;
    matchedQty += takeQty;
    remainingByContract.set(key, remainingQty - takeQty);

    // Track earliest entry time among matched fills
    if (fill.time) {
      if (!earliestEntryTime) {
        earliestEntryTime = fill.time;
      } else {
        const fillTime = Date.parse(fill.time);
        const currentEarliest = Date.parse(earliestEntryTime);
        if (
          !Number.isNaN(fillTime) &&
          !Number.isNaN(currentEarliest) &&
          fillTime < currentEarliest
        ) {
          earliestEntryTime = fill.time;
        }
      }
    }
  }

  const fullyMatched = [...remainingByContract.values()].every(
    (remainingQty) => remainingQty <= 0,
  );
  if (!fullyMatched || matchedQty <= 0)
    return { entryPrice: null, entryNotional: 0, entryTime: null };

  const comboUnits = Math.max(group.totalQuantity, 1);
  return {
    entryPrice: -(netCash / comboUnits),
    entryNotional: Math.abs(netCash) * 100,
    entryTime: earliestEntryTime,
  };
}

/** Build share data for a position group (aggregated fills).
 *  For BAG/combo closing groups, uses the matching opening group's net combo
 *  price as cost basis for accurate P&L % (e.g. risk reversal opened at $0.25
 *  credit, closed at $2.50 = +900%, not the misleading ~21% from leg notionals).
 *
 *  @param group - The position fill group to build share data for
 *  @param allGroups - All position groups (for finding matching opening fills)
 *  @param portfolioPositions - Portfolio positions (fallback for entry data when opening fills not in allGroups)
 */
export type PositionFillGroup = {
  id: string;
  symbol: string;
  description: string;
  isClosing: boolean;
  totalQuantity: number;
  netPrice: number | null;
  totalCommission: number;
  totalPnL: number | null;
  time: string;
  fills: ExecutedOrder[];
};

function deriveGroupDescription(
  fills: ExecutedOrder[],
  isClosing: boolean,
  portfolioPositions?: readonly PortfolioPosition[],
): string {
  return buildExecutedGroupDescription(fills, isClosing, portfolioPositions);
}

function groupExecutedOrders(
  fills: ExecutedOrder[],
  portfolioPositions?: readonly PortfolioPosition[],
): PositionFillGroup[] {
  if (fills.length === 0) return [];

  // Separate cancelled orders (keep as-is, ungrouped)
  const cancelled = fills.filter((f) => f.side === "CANCELLED");
  const real = fills.filter((f) => f.side !== "CANCELLED");

  const isClosingFill = (fill: ExecutedOrder): boolean =>
    fill.contract.secType === "OPT" &&
    fill.realizedPNL != null &&
    Math.abs(fill.realizedPNL) > 0.01;

  type MinuteBucket = {
    symbol: string;
    opens: ExecutedOrder[];
    closes: ExecutedOrder[];
    bags: ExecutedOrder[];
  };

  const byMinute = new Map<string, MinuteBucket>();
  for (const fill of real) {
    const sym = fill.contract.symbol;
    const t = new Date(fill.time);
    // Round time to nearest minute for grouping
    const bucket = new Date(
      t.getFullYear(),
      t.getMonth(),
      t.getDate(),
      t.getHours(),
      t.getMinutes(),
    ).toISOString();
    const key = `${sym}_${bucket}`;
    const bucketData = byMinute.get(key);
    if (bucketData == null) {
      byMinute.set(key, {
        symbol: sym,
        opens: [],
        closes: [],
        bags: [],
      });
    }

    const target = byMinute.get(key);
    if (!target) continue;

    if (fill.contract.secType === "BAG") {
      target.bags.push(fill);
    } else if (isClosingFill(fill)) {
      target.closes.push(fill);
    } else {
      target.opens.push(fill);
    }
  }

  const assignBagToBucket = (
    bag: ExecutedOrder,
    bucketSideFills: ExecutedOrder[],
    fallback: ExecutedOrder[],
  ): ExecutedOrder[] => {
    if (bucketSideFills.length === 0) return [...fallback];
    const bagTime = Date.parse(bag.time);
    const safeBagTime = Number.isNaN(bagTime) ? null : bagTime;
    let bestSide: ExecutedOrder[] = [];
    let bestDistance = Number.POSITIVE_INFINITY;

    for (const targetFill of bucketSideFills) {
      const targetTime = Date.parse(targetFill.time);
      if (Number.isNaN(targetTime) || safeBagTime == null) continue;
      const delta = Math.abs(targetTime - safeBagTime);
      if (delta < bestDistance) {
        bestDistance = delta;
        bestSide = [targetFill];
      }
    }

    if (!Number.isFinite(bestDistance)) return [...fallback];
    return bestSide;
  };

  const makeGroup = (
    groupFills: ExecutedOrder[],
    isClosing: boolean,
  ): PositionFillGroup => {
    const optFills = groupFills.filter((f) => f.contract.secType !== "BAG");
    const sym = groupFills[0].contract.symbol;
    const bagFills = groupFills.filter((f) => f.contract.secType === "BAG");
    const totalQty =
      bagFills.length > 0
        ? bagFills.reduce((sum, f) => sum + f.quantity, 0)
        : optFills.reduce((sum, f) => sum + f.quantity, 0);

    // Net price: BAG fill has the combo price, single-leg uses weighted avg
    let netPrice: number | null = null;
    if (bagFills.length > 0 && bagFills[0].avgPrice != null) {
      netPrice = bagFills[0].avgPrice;
    } else if (optFills.length > 0) {
      const totalQty = optFills.reduce((s, f) => s + f.quantity, 0);
      const weightedSum = optFills.reduce(
        (s, f) => s + (f.avgPrice ?? 0) * f.quantity,
        0,
      );
      netPrice =
        totalQty > 0 ? Number((weightedSum / totalQty).toFixed(4)) : null;
    }

    const totalCommission = optFills.reduce(
      (sum, f) => sum + (f.commission ?? 0),
      0,
    );
    const totalPnL = isClosing
      ? optFills.reduce((sum, f) => sum + (f.realizedPNL ?? 0), 0)
      : null;

    const latestTime = groupFills.reduce((maxTime, f) => {
      const current = Date.parse(f.time);
      const previous = Date.parse(maxTime);
      if (Number.isNaN(current)) return maxTime;
      if (Number.isNaN(previous)) return f.time;
      return current > previous ? f.time : maxTime;
    }, groupFills[0].time);

    return {
      id: `${sym}_${Date.parse(groupFills[0].time).toString()}`,
      symbol: sym,
      description: deriveGroupDescription(
        groupFills,
        isClosing,
        portfolioPositions,
      ),
      isClosing,
      totalQuantity: totalQty,
      netPrice,
      totalCommission,
      totalPnL,
      time: latestTime,
      fills: groupFills,
    };
  };

  const nextId = (() => {
    let id = 0;
    return () => {
      id += 1;
      return `position-group-${id}`;
    };
  })();

  const result: PositionFillGroup[] = [];
  for (const bucket of byMinute.values()) {
    const { opens, closes, bags } = bucket;
    if (opens.length > 0 && closes.length > 0) {
      const closeBuckets: ExecutedOrder[] = [];
      const openBuckets: ExecutedOrder[] = [];

      for (const bag of bags) {
        const closeDistances = assignBagToBucket(bag, closes, closes);
        const openDistances = assignBagToBucket(bag, opens, opens);
        if (closeDistances.length > 0 && openDistances.length > 0) {
          // If both have valid distances, pick the nearer side.
          const bagTime = Date.parse(bag.time);
          const closeDist = closeDistances.map((f) =>
            Math.abs(Date.parse(f.time) - bagTime),
          )[0];
          const openDist = openDistances.map((f) =>
            Math.abs(Date.parse(f.time) - bagTime),
          )[0];
          if (closeDist <= openDist) closeBuckets.push(bag);
          else openBuckets.push(bag);
        } else if (closeDistances.length > 0) {
          closeBuckets.push(bag);
        } else {
          openBuckets.push(bag);
        }
      }

      const closeGroupFills = [...closes, ...closeBuckets];
      const openGroupFills = [...opens, ...openBuckets];

      if (closeGroupFills.length > 0) {
        result.push({
          ...makeGroup(closeGroupFills, true),
          id: `${nextId()}-close`,
        });
      }
      if (openGroupFills.length > 0) {
        result.push({
          ...makeGroup(openGroupFills, false),
          id: `${nextId()}-open`,
        });
      }
      continue;
    }

    if (closes.length > 0) {
      result.push({
        ...makeGroup([...closes, ...bags], true),
        id: `${nextId()}-close`,
      });
    } else if (opens.length > 0) {
      result.push({
        ...makeGroup([...opens, ...bags], false),
        id: `${nextId()}-open`,
      });
    }
  }

  // Add cancelled orders as individual groups
  for (const c of cancelled) {
    result.push({
      id: c.execId,
      symbol: c.contract.symbol || c.symbol,
      description: `Cancelled ${c.symbol}`,
      isClosing: false,
      totalQuantity: c.quantity,
      netPrice: c.avgPrice,
      totalCommission: 0,
      totalPnL: null,
      time: c.time,
      fills: [c],
    });
  }

  // Sort by latest execution time descending
  result.sort((a, b) => {
    const bMs = Date.parse(b.time);
    const aMs = Date.parse(a.time);
    if (Number.isNaN(aMs) && Number.isNaN(bMs)) return 0;
    if (Number.isNaN(aMs)) return 1;
    if (Number.isNaN(bMs)) return -1;
    return bMs - aMs;
  });
  return result;
}

export function executedGroupDetailFills(
  group: PositionFillGroup,
): ExecutedOrder[] {
  const executableLegs = group.fills.filter(
    (fill) => fill.contract.secType !== "BAG",
  );
  return executableLegs.length > 0 ? executableLegs : group.fills;
}

function SortTh<K extends string>({
  label,
  sortKey,
  activeKey,
  direction,
  onToggle,
  className,
}: {
  label: string;
  sortKey: K;
  activeKey: K | null;
  direction: SortDirection;
  onToggle: (key: K) => void;
  className?: string;
}) {
  const active = activeKey === sortKey;
  const ariaSort = active
    ? direction === "asc"
      ? "ascending"
      : "descending"
    : undefined;
  return (
    <th
      className={`sortable-th ${className ?? ""} ${active ? "sort-active" : ""}`}
      onClick={() => onToggle(sortKey)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onToggle(sortKey);
        }
      }}
      tabIndex={0}
      role="columnheader"
      aria-sort={ariaSort}
    >
      <span className="sort-label">
        {label}
        <span className="sort-icon">
          {active ? (
            direction === "asc" ? (
              <ChevronUp size={10} />
            ) : (
              <ChevronDown size={10} />
            )
          ) : (
            <ChevronDown size={10} className="sort-icon-idle" />
          )}
        </span>
      </span>
    </th>
  );
}

/* ─── Price direction hook (local, used by OrderPriceCell) ── */

function usePriceDirection(price: number | null): {
  direction: "up" | "down" | null;
  flashDirection: "up" | "down" | null;
} {
  const [direction, setDirection] = useState<"up" | "down" | null>(null);
  const [flashDirection, setFlashDirection] = useState<"up" | "down" | null>(
    null,
  );
  const previousPrice = useRef<number | null>(null);

  useEffect(() => {
    const previous = previousPrice.current;

    if (previous == null || price == null) {
      setDirection(null);
      setFlashDirection(null);
      previousPrice.current = price;
      return undefined;
    }

    if (price > previous) {
      setDirection("up");
      setFlashDirection("up");
    } else if (price < previous) {
      setDirection("down");
      setFlashDirection("down");
    } else {
      setFlashDirection(null);
    }

    previousPrice.current = price;

    if (price !== previous) {
      const timer = setTimeout(() => setFlashDirection(null), 2500);
      return () => clearTimeout(timer);
    }
    return undefined;
  }, [price]);

  return { direction, flashDirection };
}

type PortfolioViewMode = "risk" | "structure";
const PORTFOLIO_VIEW_KEY = "xenon.portfolio.view";

function PortfolioSections({
  portfolio,
  prices,
  activeAccount = "ib",
}: {
  portfolio: PortfolioData | null;
  prices?: Record<string, PriceData>;
  activeAccount?: "ib" | "futu";
}) {
  const positions = useMemo(
    () => portfolio?.positions ?? EMPTY_POSITIONS,
    [portfolio?.positions],
  );
  const definedPositions = useMemo(
    () => positions.filter((p) => p.risk_profile === "defined"),
    [positions],
  );
  const equityPositions = useMemo(
    () => positions.filter((p) => p.risk_profile === "equity"),
    [positions],
  );
  const undefinedPositions = useMemo(
    () =>
      positions.filter(
        (p) => p.risk_profile === "undefined" || p.risk_profile === "complex",
      ),
    [positions],
  );

  const extractPositionSearchText = useCallback(
    (p: PortfolioPosition) =>
      `${p.ticker} ${p.structure} ${p.direction} ${p.expiry}`,
    [],
  );
  // All four filter hooks declared unconditionally (React rule-of-hooks).
  const definedFilter = useTableFilter(
    definedPositions,
    extractPositionSearchText,
  );
  const undefinedFilter = useTableFilter(
    undefinedPositions,
    extractPositionSearchText,
  );
  const equityFilter = useTableFilter(
    equityPositions,
    extractPositionSearchText,
  );
  const structureFilter = useTableFilter(positions, extractPositionSearchText);

  // View mode hydration: null until we've read localStorage to avoid SSR flash.
  const [viewMode, setViewMode] = useState<PortfolioViewMode | null>(null);
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(PORTFOLIO_VIEW_KEY);
      setViewMode(
        stored === "risk" || stored === "structure" ? stored : "structure",
      );
    } catch {
      setViewMode("structure");
    }
  }, []);

  const updateMode = (m: PortfolioViewMode) => {
    setViewMode(m);
    try {
      window.localStorage.setItem(PORTFOLIO_VIEW_KEY, m);
    } catch {
      // Safari private mode — ignore.
    }
  };

  const toggleHeader = (
    <div className="section" data-testid="portfolio-view-toggle">
      <div className="section-header">
        <div className="section-title">
          <Circle size={14} />
          Portfolio
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          {viewMode === "structure" && portfolio ? (
            <TableSearch
              query={structureFilter.query}
              setQuery={structureFilter.setQuery}
              placeholder="Filter positions..."
              resultCount={structureFilter.filtered.length}
              totalCount={positions.length}
            />
          ) : null}
          <div
            role="tablist"
            aria-label="Portfolio view"
            style={{ display: "flex", gap: "4px" }}
          >
            <button
              type="button"
              role="tab"
              aria-selected={viewMode === "structure"}
              disabled={!portfolio}
              className={`pill ${viewMode === "structure" ? "defined" : "neutral"}`}
              onClick={() => updateMode("structure")}
              data-testid="toggle-by-structure"
            >
              By Structure
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={viewMode === "risk"}
              disabled={!portfolio}
              className={`pill ${viewMode === "risk" ? "defined" : "neutral"}`}
              onClick={() => updateMode("risk")}
              data-testid="toggle-by-risk"
            >
              By Risk
            </button>
          </div>
        </div>
      </div>
    </div>
  );

  if (!portfolio) {
    return (
      <>
        {toggleHeader}
        <div className="section">
          <div className="section-header">
            <div className="section-title">
              <Circle size={14} />
              Portfolio
              <InfoTooltip text={SECTION_TOOLTIPS["Defined Risk Positions"]} />
            </div>
            <span className="pill neutral">LOADING</span>
          </div>
          <div className="section-body">
            <div className="alert-item">Waiting for portfolio data...</div>
          </div>
        </div>
      </>
    );
  }

  // Hydration pending — show neutral shell under the header.
  if (viewMode === null) {
    return (
      <>
        {toggleHeader}
        <div className="section">
          <div className="section-body">
            <div className="alert-item">Loading view…</div>
          </div>
        </div>
      </>
    );
  }

  if (viewMode === "structure") {
    return (
      <>
        {toggleHeader}
        <PortfolioByStructure
          positions={structureFilter.filtered}
          prices={prices}
          activeAccount={activeAccount}
          lastSync={portfolio.last_sync}
        />
      </>
    );
  }

  return (
    <>
      {toggleHeader}
      {definedPositions.length > 0 && (
        <div className="section">
          <div className="section-header">
            <div className="section-title">
              <CheckCircle2 size={14} />
              Defined Risk Positions
              <InfoTooltip text={SECTION_TOOLTIPS["Defined Risk Positions"]} />
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <TableSearch
                query={definedFilter.query}
                setQuery={definedFilter.setQuery}
                placeholder="Filter positions..."
                resultCount={definedFilter.filtered.length}
                totalCount={definedPositions.length}
              />
              <span className="pill defined">
                {definedPositions.length} POSITIONS
              </span>
            </div>
          </div>
          <div className="section-body">
            <PositionTable
              positions={definedFilter.filtered}
              showUnderlying={true}
              prices={prices}
              readonly={activeAccount === "futu"}
            />
          </div>
        </div>
      )}

      {undefinedPositions.length > 0 && (
        <div className="section">
          <div className="section-header">
            <div className="section-title">
              <TriangleAlert size={14} />
              Undefined Risk Positions
              <InfoTooltip
                text={SECTION_TOOLTIPS["Undefined Risk Positions"]}
              />
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <TableSearch
                query={undefinedFilter.query}
                setQuery={undefinedFilter.setQuery}
                placeholder="Filter positions..."
                resultCount={undefinedFilter.filtered.length}
                totalCount={undefinedPositions.length}
              />
              <span className="pill undefined">
                {undefinedPositions.length} POSITIONS
              </span>
            </div>
          </div>
          <div className="section-body">
            <PositionTable
              positions={undefinedFilter.filtered}
              showUnderlying={true}
              prices={prices}
              readonly={activeAccount === "futu"}
            />
          </div>
        </div>
      )}

      {equityPositions.length > 0 && (
        <div className="section">
          <div className="section-header">
            <div className="section-title">
              <Circle size={14} />
              Equity Positions
              <InfoTooltip text={SECTION_TOOLTIPS["Equity Positions"]} />
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <TableSearch
                query={equityFilter.query}
                setQuery={equityFilter.setQuery}
                placeholder="Filter positions..."
                resultCount={equityFilter.filtered.length}
                totalCount={equityPositions.length}
              />
              <span className="pill neutral">
                {equityPositions.length} POSITIONS
              </span>
            </div>
          </div>
          <div className="section-body">
            <PositionTable
              positions={equityFilter.filtered}
              showExpiry={false}
              prices={prices}
              readonly={activeAccount === "futu"}
            />
          </div>
        </div>
      )}

      <div className="section">
        <div className="report-meta">
          Last Sync: {new Date(portfolio.last_sync).toLocaleString()} • Source:{" "}
          {activeAccount === "futu" ? "Futu OpenD" : "IB Gateway"}
        </div>
      </div>
    </>
  );
}

const EMPTY_POSITIONS: readonly PortfolioPosition[] = Object.freeze([]);

// Hoisted formatters — module scope so they have stable identity across renders.
type JournalSortKey =
  | "id"
  | "date"
  | "ticker"
  | "structure"
  | "decision"
  | "qty"
  | "entry_cost"
  | "max_risk"
  | "realized_pnl"
  | "ror";

const journalSortExtract = (
  t: TradeEntry,
  key: JournalSortKey,
): string | number | null => {
  switch (key) {
    case "id":
      return t.id;
    case "date":
      return t.date;
    case "ticker":
      return t.ticker;
    case "structure":
      return t.structure;
    case "decision":
      return t.decision;
    case "qty":
      return t.contracts ?? t.shares ?? t.quantity ?? null;
    case "entry_cost":
      return t.total_cost ?? t.entry_cost ?? null;
    case "max_risk":
      return t.max_risk ?? null;
    case "realized_pnl":
      return t.realized_pnl ?? null;
    case "ror":
      return t.return_on_risk ?? null;
    default:
      return null;
  }
};

const JournalSections = React.memo(function JournalSections() {
  const { data, loading, error, syncWithIB, syncing, lastSyncResult } =
    useJournal();
  const [syncError, setSyncError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const trades = useMemo(() => {
    if (!data?.trades) return [];
    return [...data.trades].sort((a, b) => b.id - a.id);
  }, [data]);

  const extractSearchText = useCallback(
    (t: TradeEntry) =>
      `${t.ticker} ${t.structure} ${t.decision} ${t.date} ${t.edge_analysis?.edge_type ?? ""}`,
    [],
  );
  const { filtered, query, setQuery } = useTableFilter(
    trades,
    extractSearchText,
  );
  const {
    sorted: sortedTrades,
    sort,
    toggle,
  } = useSort(filtered, journalSortExtract, "id" as JournalSortKey, "desc");

  const toggleExpand = useCallback((id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleSync = useCallback(async () => {
    setSyncError(null);
    try {
      await syncWithIB();
    } catch (e) {
      setSyncError(e instanceof Error ? e.message : "Sync failed");
    }
  }, [syncWithIB]);

  const fmtJournalUsd = (v: number | undefined | null) => {
    if (v == null) return "—";
    const abs = Math.abs(v);
    const formatted =
      abs >= 1000
        ? `$${abs.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`
        : `$${abs.toFixed(2)}`;
    return v < 0 ? `-${formatted}` : formatted;
  };

  const decisionClass = (d: string) => {
    if (d === "EXECUTED" || d === "OPEN") return "bullish";
    if (d === "CLOSED") return "neutral";
    if (d === "FREED" || d === "CONVERTED") return "lean-bullish";
    if (d === "IB_AUTO_IMPORT") return "ib-import";
    return "bearish";
  };

  const pnlClass = (v: number | undefined | null) => {
    if (v == null) return "";
    return v >= 0 ? "bullish" : "bearish";
  };

  return (
    <>
      <div className="section">
        <div className="section-header">
          <div className="section-title">
            <Wrench size={14} />
            Trade Journal
            <InfoTooltip text={SECTION_TOOLTIPS["Trade Journal"]} />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <button
              className="btn-sync"
              onClick={handleSync}
              disabled={syncing}
              title="Sync unreconciled IB trades into journal"
            >
              {syncing ? "SYNCING..." : "SYNC IB"}
            </button>
            {lastSyncResult && (
              <span className="pill defined" style={{ fontSize: "9px" }}>
                {lastSyncResult.imported > 0
                  ? `+${lastSyncResult.imported} IMPORTED`
                  : "UP TO DATE"}
              </span>
            )}
            {trades.length > 0 ? (
              <TableSearch
                query={query}
                setQuery={setQuery}
                placeholder="Filter trades..."
                resultCount={filtered.length}
                totalCount={trades.length}
              />
            ) : null}
            <span className="pill defined">{trades.length} TRADES</span>
          </div>
        </div>
        {error && (
          <div className="section-body">
            <div className="alert-item bearish">{error}</div>
          </div>
        )}
        {syncError && (
          <div className="section-body">
            <div className="alert-item bearish">IB Sync: {syncError}</div>
          </div>
        )}
        {loading && (
          <div className="section-body p-6">
            <TableSkeleton rows={4} columns={6} />
          </div>
        )}
        {!loading && trades.length === 0 && !error && (
          <div className="section-body">
            <div className="alert-item">No trades in journal.</div>
          </div>
        )}
        {trades.length > 0 && (
          <div className="section-body table-wrap">
            <table>
              <thead>
                <tr>
                  <SortTh<JournalSortKey>
                    label="#"
                    sortKey="id"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<JournalSortKey>
                    label="Date"
                    sortKey="date"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<JournalSortKey>
                    label="Ticker"
                    sortKey="ticker"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<JournalSortKey>
                    label="Structure"
                    sortKey="structure"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<JournalSortKey>
                    label="Status"
                    sortKey="decision"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<JournalSortKey>
                    label="Qty"
                    sortKey="qty"
                    className="right"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<JournalSortKey>
                    label="Entry Cost"
                    sortKey="entry_cost"
                    className="right"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<JournalSortKey>
                    label="Max Risk"
                    sortKey="max_risk"
                    className="right"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<JournalSortKey>
                    label="Realized P&L"
                    sortKey="realized_pnl"
                    className="right"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<JournalSortKey>
                    label="RoR"
                    sortKey="ror"
                    className="right"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <th>Gates</th>
                  <th>Edge</th>
                </tr>
              </thead>
              <tbody>
                {sortedTrades.map((t) => {
                  const qty = t.contracts ?? t.shares ?? t.quantity ?? null;
                  const cost = t.total_cost ?? t.entry_cost ?? null;
                  const hasLegs = t.legs && t.legs.length > 0;
                  const isExpanded = expandedIds.has(t.id);
                  return (
                    <React.Fragment key={t.id}>
                      <tr>
                        <td className="cell-muted">{t.id}</td>
                        <td>{t.date}</td>
                        <td>
                          <span
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: "4px",
                            }}
                          >
                            <TickerLink ticker={t.ticker} />
                            {hasLegs && (
                              <button
                                className="expand-btn"
                                onClick={() => toggleExpand(t.id)}
                                title={
                                  isExpanded ? "Collapse legs" : "Expand legs"
                                }
                              >
                                <ChevronDown
                                  size={12}
                                  style={{
                                    transform: isExpanded
                                      ? "rotate(180deg)"
                                      : "rotate(0deg)",
                                    transition: "transform 150ms ease",
                                  }}
                                />
                              </button>
                            )}
                          </span>
                        </td>
                        <td>{t.structure}</td>
                        <td>
                          <span className={decisionClass(t.decision)}>
                            {t.decision}
                          </span>
                        </td>
                        <td className="right">{qty ?? "—"}</td>
                        <td className="right">{fmtJournalUsd(cost)}</td>
                        <td className="right">{fmtJournalUsd(t.max_risk)}</td>
                        <td className="right">
                          <span className={pnlClass(t.realized_pnl)}>
                            {fmtJournalUsd(t.realized_pnl)}
                            {t.return_on_risk != null
                              ? ` (${t.return_on_risk * 100 >= 0 ? "+" : ""}${(t.return_on_risk * 100).toFixed(1)}%)`
                              : ""}
                          </span>
                        </td>
                        <td className="right">
                          {t.return_on_risk != null
                            ? `${(t.return_on_risk * 100).toFixed(1)}%`
                            : "—"}
                        </td>
                        <td className="cell-muted">
                          {t.gates_passed?.join(", ") ||
                            t.gates_failed?.join(", ") ||
                            "—"}
                        </td>
                        <td className="cell-muted">
                          {t.edge_analysis?.edge_type ?? "—"}
                        </td>
                      </tr>
                      {hasLegs &&
                        isExpanded &&
                        t.legs!.map((leg, i) => (
                          <tr key={`${t.id}-leg-${i}`} className="leg-row">
                            <td />
                            <td className="cell-muted">{leg.expiry ?? "—"}</td>
                            <td />
                            <td className="cell-muted">
                              {leg.type ?? "—"}
                              {leg.strike != null ? ` $${leg.strike}` : ""}
                            </td>
                            <td />
                            <td className="right cell-muted">
                              {leg.contracts ?? "—"}
                            </td>
                            <td className="right cell-muted">
                              {leg.open_price != null
                                ? `$${leg.open_price.toFixed(2)}`
                                : "—"}
                            </td>
                            <td className="right cell-muted">
                              {leg.close_price != null
                                ? `$${leg.close_price.toFixed(2)}`
                                : "—"}
                            </td>
                            <td className="right">
                              <span className={pnlClass(leg.leg_pnl)}>
                                {leg.leg_pnl != null
                                  ? fmtJournalUsd(leg.leg_pnl)
                                  : "—"}
                              </span>
                            </td>
                            <td />
                            <td />
                            <td />
                          </tr>
                        ))}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
});

/* ─── Orders tables ────────────────────────────────────── */

type OpenOrderKey =
  | "symbol"
  | "action"
  | "orderType"
  | "totalQuantity"
  | "limitPrice"
  | "lastPrice"
  | "status"
  | "tif"
  | "actions";

/** Build the prices-map key for an order's contract (option key for options, symbol for stocks). */
function orderPriceKey(contract: OpenOrder["contract"]): string | null {
  if (contract.secType === "BAG") return null;

  if (
    contract.secType === "OPT" &&
    contract.strike != null &&
    contract.right &&
    contract.expiry
  ) {
    const right =
      contract.right === "C" || contract.right === "P"
        ? contract.right
        : contract.right === "CALL"
          ? "C"
          : contract.right === "PUT"
            ? "P"
            : null;
    if (right) {
      const expiryClean = contract.expiry.replace(/-/g, "");
      if (expiryClean.length === 8) {
        return optionKey({
          symbol: contract.symbol.toUpperCase(),
          expiry: expiryClean,
          strike: contract.strike,
          right,
        });
      }
    }
  }
  return contract.symbol;
}

/**
 * Resolve the "last price" for an order.
 * For STK/OPT: use the WS price directly.
 * For BAG (spread): find the matching portfolio position and compute
 * the net mid from each leg's WS bid/ask (long leg mid − short leg mid).
 */
function resolveOrderLastPrice(
  order: OpenOrder,
  prices: Record<string, PriceData> | undefined,
  portfolio: PortfolioData | null | undefined,
): number | null {
  if (!prices) return null;
  const pk = orderPriceKey(order.contract);
  if (pk) return prices[pk]?.last ?? null;

  // BAG: compute net mid from portfolio legs
  if (order.contract.secType !== "BAG" || !portfolio) return null;
  const pos = portfolio.positions.find(
    (p) => p.ticker === order.contract.symbol && p.legs.length > 1,
  );
  if (!pos) return null;

  let netMid = 0;
  for (const leg of pos.legs) {
    const key = legPriceKey(pos.ticker, pos.expiry, leg);
    if (!key) return null;
    const lp = prices[key];
    if (!lp || lp.bid == null || lp.ask == null) return null;
    const mid = (lp.bid + lp.ask) / 2;
    const sign = leg.direction === "LONG" ? 1 : -1;
    netMid += sign * mid;
  }
  return Math.round(netMid * 100) / 100;
}

function makeOpenOrderExtract(
  prices?: Record<string, PriceData>,
  portfolio?: PortfolioData | null,
) {
  return (
    item: OpenOrderDisplayRow,
    key: OpenOrderKey,
  ): string | number | null => {
    switch (key) {
      case "symbol": {
        return item.kind === "combo" ? item.symbol : item.order.contract.symbol;
      }
      case "action":
        return item.kind === "combo" ? "COMBO" : item.order.action;
      case "orderType":
        return item.kind === "combo" ? item.structure : item.order.orderType;
      case "totalQuantity":
        return item.kind === "combo"
          ? item.totalQuantity
          : item.order.totalQuantity;
      case "limitPrice":
        return item.kind === "combo" ? item.limitPrice : item.order.limitPrice;
      case "lastPrice":
        return item.kind === "combo"
          ? resolveOpenOrderComboPrice(item.orders, prices)
          : resolveOrderLastPrice(item.order, prices, portfolio);
      case "status":
        return item.kind === "combo" ? item.status : item.order.status;
      case "tif":
        return item.kind === "combo" ? item.tif : item.order.tif;
      case "actions":
        return null;
      default:
        return null;
    }
  };
}

/** Wrapper so usePriceDirection can be called per-order row (hooks can't go in map callbacks). */
function OrderPriceCell({ price }: { price: number | null }) {
  const { direction, flashDirection } = usePriceDirection(price);
  return (
    <td
      className={`right last-price-cell ${flashDirection ? `last-price-${flashDirection}` : ""}`}
    >
      {price != null ? fmtPrice(price) : "—"}
      {direction === "up" && (
        <ArrowUp
          size={11}
          className="price-trend-icon price-trend-up"
          aria-label="price up"
        />
      )}
      {direction === "down" && (
        <ArrowDown
          size={11}
          className="price-trend-icon price-trend-down"
          aria-label="price down"
        />
      )}
    </td>
  );
}

type ExecOrderKey =
  | "symbol"
  | "side"
  | "quantity"
  | "avgPrice"
  | "commission"
  | "realizedPNL"
  | "time";

const execOrderExtract = (
  item: ExecutedOrder,
  key: ExecOrderKey,
): string | number | null => {
  switch (key) {
    case "symbol":
      return item.symbol;
    case "side":
      return item.side;
    case "quantity":
      return item.quantity;
    case "avgPrice":
      return item.avgPrice;
    case "commission":
      return item.commission;
    case "realizedPNL":
      return item.realizedPNL;
    case "time":
      return item.time;
    default:
      return null;
  }
};

function OrdersSections({
  orders,
  prices,
  portfolio,
}: {
  orders: OrdersData | null;
  prices?: Record<string, PriceData>;
  portfolio?: PortfolioData | null;
}) {
  const {
    pendingCancels,
    pendingModifies,
    cancelledOrders,
    requestCancel,
    requestModify,
  } = useOrderActions();
  const openOrderExtract = useMemo(
    () => makeOpenOrderExtract(prices, portfolio),
    [prices, portfolio],
  );
  const openOrderRows = useMemo(() => {
    if (!orders) return [];
    return buildOpenOrderDisplayRows(orders.open_orders, portfolio?.positions);
  }, [orders, portfolio?.positions]);
  const openSort = useSort(openOrderRows, openOrderExtract);
  const extractOpenSearch = useCallback((row: OpenOrderDisplayRow) => {
    if (row.kind === "combo") return `${row.symbol} ${row.structure} combo`;
    return `${row.order.contract.symbol} ${row.order.action} ${row.order.orderType}`;
  }, []);
  const openFilter = useTableFilter(openSort.sorted, extractOpenSearch);

  const [cancelTarget, setCancelTarget] = useState<OpenOrder | null>(null);
  const [modifyTarget, setModifyTarget] = useState<{
    modalOrder: OpenOrder;
    requestOrder: OpenOrder;
    cancelOrders?: ModifyOrderRequest["cancelOrders"];
  } | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  const handleCancel = useCallback(async () => {
    if (!cancelTarget) return;
    setActionLoading(true);
    const result = await requestCancel(cancelTarget);
    setActionLoading(false);
    // B4 — keep modal open on failure so the user sees the FAILED pill on the
    // row and the toast. Only dismiss on a clean 2xx.
    if (result.ok) {
      setCancelTarget(null);
    }
  }, [cancelTarget, requestCancel]);

  const handleModify = useCallback(
    async (request: ModifyOrderRequest) => {
      if (!modifyTarget) return;
      setActionLoading(true);
      const result = await requestModify(
        modifyTarget.requestOrder,
        modifyTarget.cancelOrders?.length
          ? { ...request, cancelOrders: modifyTarget.cancelOrders }
          : request,
      );
      setActionLoading(false);
      if (result.ok) {
        setModifyTarget(null);
      }
    },
    [modifyTarget, requestModify],
  );

  const handleCancelCombo = useCallback(
    async (comboOrders: OpenOrder[]) => {
      setActionLoading(true);
      try {
        for (const order of comboOrders) {
          await requestCancel(order);
        }
      } finally {
        setActionLoading(false);
      }
    },
    [requestCancel],
  );

  // Merge cancelled orders into executed list for display (dedupe by permId)
  const allExecutedRows = useMemo(() => {
    const seen = new Set<number>();
    const cancelRows: ExecutedOrder[] = [];
    for (const c of cancelledOrders) {
      if (seen.has(c.permId)) continue;
      seen.add(c.permId);
      cancelRows.push({
        execId: `cancelled-${c.permId}`,
        symbol: c.symbol,
        contract: {
          conId: null,
          symbol: c.symbol,
          secType: "",
          strike: null,
          right: null,
          expiry: null,
        },
        side: "CANCELLED",
        quantity: c.totalQuantity,
        avgPrice: c.limitPrice,
        commission: null,
        realizedPNL: null,
        time: c.cancelledAt,
        exchange: "",
      });
    }
    return [...cancelRows, ...(orders?.executed_orders ?? [])];
  }, [cancelledOrders, orders?.executed_orders]);

  const execSortWithCancelled = useSort<ExecutedOrder, ExecOrderKey>(
    allExecutedRows,
    execOrderExtract,
    "time",
    "desc",
  );

  // Group fills into position-level rows
  const positionGroups = useMemo(
    () => groupExecutedOrders(allExecutedRows, portfolio?.positions),
    [allExecutedRows, portfolio?.positions],
  );

  const extractExecSearch = useCallback(
    (g: PositionFillGroup) => `${g.symbol} ${g.description}`,
    [],
  );
  const execFilter = useTableFilter(positionGroups, extractExecSearch);

  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const toggleGroup = useCallback((groupId: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  }, []);

  if (!orders) {
    return (
      <div className="section">
        <div className="section-header">
          <div className="section-title">
            <ClipboardList size={14} />
            Orders
            <InfoTooltip text={SECTION_TOOLTIPS["Open Orders"]} />
          </div>
          <span className="pill neutral">LOADING</span>
        </div>
        <div className="section-body">
          <div className="alert-item">Waiting for orders data...</div>
        </div>
      </div>
    );
  }

  const canModify = (o: OpenOrder) =>
    o.orderType === "LMT" || o.orderType === "STP LMT";
  const execCount = orders.executed_count + cancelledOrders.length;

  return (
    <>
      <CancelOrderDialog
        order={cancelTarget}
        loading={actionLoading}
        onConfirm={handleCancel}
        onClose={() => setCancelTarget(null)}
      />
      <ModifyOrderModal
        order={modifyTarget?.modalOrder ?? null}
        loading={actionLoading}
        prices={prices}
        portfolio={portfolio}
        onConfirm={handleModify}
        onClose={() => setModifyTarget(null)}
      />

      <div className="section">
        <div className="section-header">
          <div className="section-title">
            <ClipboardList size={14} />
            Open Orders
            <InfoTooltip text={SECTION_TOOLTIPS["Open Orders"]} />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <TableSearch
              query={openFilter.query}
              setQuery={openFilter.setQuery}
              placeholder="Filter orders..."
              resultCount={openFilter.filtered.length}
              totalCount={openSort.sorted.length}
            />
            <span className="pill defined">{orders.open_count} ORDERS</span>
          </div>
        </div>
        <div className="section-body">
          {openOrderRows.length === 0 ? (
            <div className="alert-item">No open orders</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <SortTh<OpenOrderKey>
                    label="Symbol"
                    sortKey="symbol"
                    activeKey={openSort.sort.key}
                    direction={openSort.sort.direction}
                    onToggle={openSort.toggle}
                  />
                  <SortTh<OpenOrderKey>
                    label="Action"
                    sortKey="action"
                    activeKey={openSort.sort.key}
                    direction={openSort.sort.direction}
                    onToggle={openSort.toggle}
                  />
                  <SortTh<OpenOrderKey>
                    label="Type"
                    sortKey="orderType"
                    activeKey={openSort.sort.key}
                    direction={openSort.sort.direction}
                    onToggle={openSort.toggle}
                  />
                  <SortTh<OpenOrderKey>
                    label="Quantity"
                    sortKey="totalQuantity"
                    className="right"
                    activeKey={openSort.sort.key}
                    direction={openSort.sort.direction}
                    onToggle={openSort.toggle}
                  />
                  <SortTh<OpenOrderKey>
                    label="Limit Price"
                    sortKey="limitPrice"
                    className="right"
                    activeKey={openSort.sort.key}
                    direction={openSort.sort.direction}
                    onToggle={openSort.toggle}
                  />
                  <SortTh<OpenOrderKey>
                    label="Last Price"
                    sortKey="lastPrice"
                    className="right"
                    activeKey={openSort.sort.key}
                    direction={openSort.sort.direction}
                    onToggle={openSort.toggle}
                  />
                  <SortTh<OpenOrderKey>
                    label="Status"
                    sortKey="status"
                    activeKey={openSort.sort.key}
                    direction={openSort.sort.direction}
                    onToggle={openSort.toggle}
                  />
                  <SortTh<OpenOrderKey>
                    label="TIF"
                    sortKey="tif"
                    activeKey={openSort.sort.key}
                    direction={openSort.sort.direction}
                    onToggle={openSort.toggle}
                  />
                  <th className="actions-th">Actions</th>
                </tr>
              </thead>
              <tbody>
                {openFilter.filtered.map((o) => {
                  if (o.kind === "combo") {
                    const comboCanModify = o.orders.every(canModify);
                    const comboModifyTarget = buildGroupedComboModifyTarget(o);
                    const isPendingCancel = o.orders.some((order) =>
                      pendingCancels.has(order.permId),
                    );
                    const isPendingModify = o.orders.some((order) =>
                      pendingModifies.has(order.permId),
                    );
                    const isPending = isPendingCancel || isPendingModify;

                    return (
                      <tr
                        key={o.id}
                        className={
                          isPendingCancel
                            ? "row-pending-cancel"
                            : isPendingModify
                              ? "row-pending-modify"
                              : undefined
                        }
                      >
                        <td>
                          <TickerLink ticker={o.symbol} />
                          <span
                            style={{
                              marginLeft: "8px",
                              fontFamily: "var(--font-mono)",
                              fontSize: "11px",
                              color: "var(--text-secondary)",
                            }}
                          >
                            {o.summary}
                          </span>
                          {isPending && (
                            <Loader2 size={12} className="cancel-spinner" />
                          )}
                        </td>
                        <td>
                          <span className="pill neutral">COMBO</span>
                        </td>
                        <td>{o.structure}</td>
                        <td className="right">{o.totalQuantity}</td>
                        <td className="right">
                          <span
                            className={
                              isPendingModify ? "status-modifying" : ""
                            }
                          >
                            {isPendingModify
                              ? "—"
                              : o.limitPrice != null
                                ? fmtPrice(o.limitPrice)
                                : "—"}
                          </span>
                        </td>
                        <OrderPriceCell
                          price={resolveOpenOrderComboPrice(o.orders, prices)}
                        />
                        <td>
                          {isPendingCancel ? (
                            <span className="status-cancelling">
                              Cancelling...
                            </span>
                          ) : isPendingModify ? (
                            <span className="status-modifying">
                              Modifying...
                            </span>
                          ) : (
                            o.status
                          )}
                        </td>
                        <td>{o.tif}</td>
                        <td className="actions-cell">
                          {isPending ? (
                            <span className="cancel-pending-label">
                              PENDING
                            </span>
                          ) : (
                            <>
                              <button
                                className="btn-order-action btn-modify"
                                disabled={!comboCanModify}
                                title={
                                  comboCanModify
                                    ? "Modify combo order"
                                    : "Only LMT orders can be modified"
                                }
                                onClick={() =>
                                  setModifyTarget({
                                    modalOrder: comboModifyTarget.modalOrder,
                                    requestOrder: o.orders[0],
                                    cancelOrders:
                                      comboModifyTarget.cancelOrders,
                                  })
                                }
                              >
                                MODIFY
                              </button>
                              <button
                                className="btn-order-action btn-cancel"
                                onClick={() => void handleCancelCombo(o.orders)}
                              >
                                CANCEL ALL
                              </button>
                            </>
                          )}
                        </td>
                      </tr>
                    );
                  }

                  const isPendingCancel = pendingCancels.has(o.order.permId);
                  const isPendingModify = pendingModifies.has(o.order.permId);
                  const isPending = isPendingCancel || isPendingModify;
                  return (
                    <tr
                      key={`${o.order.orderId}-${o.order.permId}`}
                      className={
                        isPendingCancel
                          ? "row-pending-cancel"
                          : isPendingModify
                            ? "row-pending-modify"
                            : undefined
                      }
                    >
                      <td>
                        <TickerLink ticker={o.order.contract.symbol} />
                        {o.summary ? (
                          <span
                            style={{
                              marginLeft: "8px",
                              fontFamily: "var(--font-mono)",
                              fontSize: "11px",
                              color: "var(--text-secondary)",
                            }}
                          >
                            {o.summary}
                          </span>
                        ) : null}
                        {isPending && (
                          <Loader2 size={12} className="cancel-spinner" />
                        )}
                      </td>
                      <td>
                        <span
                          className={`pill ${o.order.action === "BUY" ? "accum" : "distrib"}`}
                        >
                          {o.order.action}
                        </span>
                      </td>
                      <td>{o.order.orderType}</td>
                      <td className="right">{o.order.totalQuantity}</td>
                      <td className="right">
                        {isPendingModify && o.order.orderType === "STP LMT" ? (
                          <span className="status-modifying">Modifying...</span>
                        ) : o.order.limitPrice != null ? (
                          fmtPrice(o.order.limitPrice)
                        ) : (
                          "—"
                        )}
                      </td>
                      <OrderPriceCell
                        price={resolveOrderLastPrice(
                          o.order,
                          prices,
                          portfolio,
                        )}
                      />
                      <td>
                        {isPendingCancel ? (
                          <span className="status-cancelling">
                            Cancelling...
                          </span>
                        ) : isPendingModify ? (
                          <span className="status-modifying">Modifying...</span>
                        ) : (
                          o.order.status
                        )}
                      </td>
                      <td>{o.order.tif}</td>
                      <td className="actions-cell">
                        {isPending ? (
                          <span className="cancel-pending-label">PENDING</span>
                        ) : (
                          <>
                            <button
                              className="btn-order-action btn-modify"
                              disabled={!canModify(o.order)}
                              title={
                                canModify(o.order)
                                  ? "Modify limit price"
                                  : "Only LMT orders can be modified"
                              }
                              onClick={() =>
                                setModifyTarget({
                                  modalOrder: o.order,
                                  requestOrder: o.order,
                                })
                              }
                            >
                              MODIFY
                            </button>
                            <button
                              className="btn-order-action btn-cancel"
                              onClick={() => setCancelTarget(o.order)}
                            >
                              CANCEL
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="section">
        <div className="section-header">
          <div className="section-title">
            <CheckCircle2 size={14} />
            Today&apos;s Executed Orders
            <InfoTooltip text={SECTION_TOOLTIPS["Today's Executed Orders"]} />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <TableSearch
              query={execFilter.query}
              setQuery={execFilter.setQuery}
              placeholder="Filter fills..."
              resultCount={execFilter.filtered.length}
              totalCount={positionGroups.length}
            />
            <span className="pill neutral">
              {positionGroups.length}{" "}
              {positionGroups.length === 1 ? "POSITION" : "POSITIONS"}
            </span>
          </div>
        </div>
        <div className="section-body">
          {positionGroups.length === 0 ? (
            <div className="alert-item">No fills this session</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th style={{ width: "24px" }}></th>
                  <th>Position</th>
                  <th>Action</th>
                  <th className="right">Quantity</th>
                  <th className="right">Net Price</th>
                  <th className="right">Commission</th>
                  <th className="right">Realized P&L</th>
                  <th>Time</th>
                  <th style={{ width: "32px" }}></th>
                </tr>
              </thead>
              <tbody>
                {execFilter.filtered.map((group) => {
                  const isExpanded = expandedGroups.has(group.id);
                  const isCancelled = group.fills[0]?.side === "CANCELLED";
                  const detailFills = executedGroupDetailFills(group);
                  return (
                    <React.Fragment key={group.id}>
                      {/* Position group header row */}
                      <tr
                        className={`exec-group-header ${isCancelled ? "row-cancelled" : ""}`}
                        style={{
                          cursor:
                            detailFills.length > 1 ? "pointer" : "default",
                        }}
                        onClick={() =>
                          detailFills.length > 1 && toggleGroup(group.id)
                        }
                      >
                        <td style={{ width: "24px", textAlign: "center" }}>
                          {detailFills.length > 1 &&
                            (isExpanded ? (
                              <ChevronDown
                                size={14}
                                style={{ color: "var(--text-secondary)" }}
                              />
                            ) : (
                              <ChevronRight
                                size={14}
                                style={{ color: "var(--text-secondary)" }}
                              />
                            ))}
                        </td>
                        <td>
                          <TickerLink ticker={group.symbol} />
                          <span
                            style={{
                              marginLeft: "8px",
                              fontFamily: "var(--font-mono)",
                              fontSize: "11px",
                              color: "var(--text-secondary)",
                            }}
                          >
                            {group.description.replace(
                              /^(Opened|Closed)\s+\w+\s*/,
                              "",
                            )}
                          </span>
                          {isCancelled && (
                            <XCircle size={12} className="cancelled-icon" />
                          )}
                        </td>
                        <td>
                          <span
                            className={`pill ${isCancelled ? "cancelled" : group.isClosing ? "distrib" : "accum"}`}
                          >
                            {isCancelled
                              ? "CANCELLED"
                              : group.isClosing
                                ? "CLOSE"
                                : "OPEN"}
                          </span>
                        </td>
                        <td className="right">{group.totalQuantity}</td>
                        <td className="right">
                          {group.netPrice != null
                            ? fmtPrice(group.netPrice)
                            : "—"}
                        </td>
                        <td className="right">
                          {group.totalCommission !== 0
                            ? fmtPrice(group.totalCommission)
                            : "—"}
                        </td>
                        <td
                          className={`right ${group.totalPnL != null ? (group.totalPnL >= 0 ? "positive" : "negative") : ""}`}
                        >
                          {group.totalPnL != null
                            ? (() => {
                                // Return on Risk: P&L / entry notional. Entry = exit - P&L.
                                const optFills = group.fills.filter(
                                  (f) => f.contract.secType === "OPT",
                                );
                                const exitNotional = optFills.reduce(
                                  (sum, f) =>
                                    sum +
                                    Math.abs(
                                      (f.avgPrice ?? 0) * f.quantity * 100,
                                    ),
                                  0,
                                );
                                const entryNotional = Math.abs(
                                  exitNotional - group.totalPnL,
                                );
                                const pct =
                                  entryNotional > 0
                                    ? (group.totalPnL / entryNotional) * 100
                                    : null;
                                return `${group.totalPnL >= 0 ? "+" : ""}${fmtPrice(group.totalPnL)}${pct != null ? ` (${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%)` : ""}`;
                              })()
                            : "—"}
                        </td>
                        <td>{new Date(group.time).toLocaleTimeString()}</td>
                        <td />
                      </tr>
                      {/* Expanded fill detail rows */}
                      {isExpanded &&
                        detailFills.map((e, i) => {
                          const displaySide =
                            e.side === "BOT"
                              ? "BUY"
                              : e.side === "SLD"
                                ? "SELL"
                                : e.side;
                          const isBAG = e.contract.secType === "BAG";
                          return (
                            <tr
                              key={`${e.execId}-${i}`}
                              className="exec-fill-row"
                            >
                              <td></td>
                              <td style={{ paddingLeft: "24px" }}>
                                <span
                                  style={{
                                    fontFamily: "var(--font-mono)",
                                    fontSize: "11px",
                                    color: "var(--text-secondary)",
                                  }}
                                >
                                  {isBAG ? `${e.symbol}` : e.symbol}
                                </span>
                              </td>
                              <td>
                                <span
                                  className={`pill ${displaySide === "BUY" ? "accum" : "distrib"}`}
                                  style={{ fontSize: "9px" }}
                                >
                                  {displaySide}
                                </span>
                              </td>
                              <td
                                className="right"
                                style={{ color: "var(--text-secondary)" }}
                              >
                                {e.quantity}
                              </td>
                              <td
                                className="right"
                                style={{ color: "var(--text-secondary)" }}
                              >
                                {e.avgPrice != null
                                  ? fmtPrice(e.avgPrice)
                                  : "—"}
                              </td>
                              <td
                                className="right"
                                style={{ color: "var(--text-secondary)" }}
                              >
                                {e.commission != null && e.commission !== 0
                                  ? fmtPrice(e.commission)
                                  : "—"}
                              </td>
                              <td
                                className="right"
                                style={{ color: "var(--text-secondary)" }}
                              >
                                {e.realizedPNL != null &&
                                Math.abs(e.realizedPNL) > 0.01
                                  ? `${e.realizedPNL >= 0 ? "+" : ""}${fmtPrice(e.realizedPNL)}`
                                  : "—"}
                              </td>
                              <td style={{ color: "var(--text-secondary)" }}>
                                {new Date(e.time).toLocaleTimeString()}
                              </td>
                              <td></td>
                            </tr>
                          );
                        })}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {orders.last_sync && (
        <div className="section">
          <div className="report-meta">
            Last Sync: {new Date(orders.last_sync).toLocaleString()} • Source:
            IB Gateway
          </div>
        </div>
      )}

      <HistoricalTradesSection />
    </>
  );
}

/* ─── Historical Trades (Flex Query) ───────────────────── */

const BLOTTER_PAGE_SIZE = 15;

type BlotterSortKey =
  | "date"
  | "symbol"
  | "contract_desc"
  | "sec_type"
  | "status"
  | "net_quantity"
  | "total_commission"
  | "realized_pnl"
  | "cost_basis"
  | "proceeds";

function getTradeDate(item: BlotterTrade): string {
  if (item.executions.length === 0) return "";
  return item.executions[item.executions.length - 1].time;
}

const blotterExtract = (
  item: BlotterTrade,
  key: BlotterSortKey,
): string | number | null => {
  switch (key) {
    case "date":
      return getTradeDate(item);
    case "symbol":
      return item.symbol;
    case "contract_desc":
      return item.contract_desc;
    case "sec_type":
      return item.sec_type;
    case "status":
      return item.is_closed ? "Closed" : "Open";
    case "net_quantity":
      return item.total_quantity ?? item.net_quantity;
    case "total_commission":
      return item.total_commission;
    case "realized_pnl":
      return item.realized_pnl;
    case "cost_basis":
      return item.cost_basis;
    case "proceeds":
      return item.proceeds;
    default:
      return null;
  }
};

export function HistoricalTradesSection() {
  const { data, loading, syncing, error, syncNow } = useBlotter(true);
  const [page, setPage] = useState(0);

  const allTrades = useMemo(() => {
    if (!data) return [];
    // Merge closed + open trades, sorted by most recent execution date desc
    const merged = [...(data.closed_trades ?? []), ...(data.open_trades ?? [])];
    merged.sort((a, b) => {
      const aDate =
        a.executions.length > 0
          ? a.executions[a.executions.length - 1].time
          : "";
      const bDate =
        b.executions.length > 0
          ? b.executions[b.executions.length - 1].time
          : "";
      return bDate.localeCompare(aDate);
    });
    return merged;
  }, [data]);

  const extractSearchText = useCallback((item: BlotterTrade) => {
    const latestExecTime = getTradeDate(item);
    return `${item.symbol} ${item.contract_desc} ${item.sec_type} ${item.is_closed ? "closed" : "open"} ${latestExecTime}`;
  }, []);

  const { filtered, query, setQuery } = useTableFilter(
    allTrades,
    extractSearchText,
  );
  const { sorted, sort, toggle } = useSort(filtered, blotterExtract);

  const totalPages = Math.max(1, Math.ceil(sorted.length / BLOTTER_PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const pageRows = sorted.slice(
    safePage * BLOTTER_PAGE_SIZE,
    (safePage + 1) * BLOTTER_PAGE_SIZE,
  );

  // Reset page when data changes
  useEffect(() => {
    setPage(0);
  }, [data, query]);

  const totalCount = allTrades.length;
  const hasData = data && (data.as_of || totalCount > 0);

  return (
    <div className="section">
      <div className="section-header">
        <div className="section-title">
          <ClipboardList size={14} />
          Historical Trades (30 Days)
          <InfoTooltip text={SECTION_TOOLTIPS["Historical Trades (30 Days)"]} />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          {data?.as_of && (
            <span
              className="report-meta"
              style={{ margin: 0, padding: 0, border: "none" }}
            >
              {new Date(data.as_of).toLocaleDateString()}
            </span>
          )}
          <SourcePill source={data?.source} />
          {allTrades.length > 0 ? (
            <TableSearch
              query={query}
              setQuery={setQuery}
              placeholder="Filter historical trades..."
              resultCount={filtered.length}
              totalCount={allTrades.length}
            />
          ) : null}
          <span className="pill neutral">{totalCount} TRADES</span>
          <button
            className="sync-button"
            disabled={syncing}
            onClick={() => syncNow()}
          >
            {syncing ? (
              <>
                <Loader2 size={12} className="spin" /> Syncing...
              </>
            ) : (
              "Refresh"
            )}
          </button>
        </div>
      </div>
      <div className="section-body">
        {error && (
          <div className="alert-item section-message bearish">{error}</div>
        )}
        {loading && (
          <div className="p-6">
            <TableSkeleton rows={5} columns={8} />
          </div>
        )}
        {!loading && !error && data?.configured === false && (
          <div className="alert-item section-message">
            <div>
              {data.message ?? "Historical trades source not configured."}
            </div>
            <div
              style={{ marginTop: "0.5rem", opacity: 0.7, fontSize: "0.85em" }}
            >
              Recent fills will populate from Postgres once the order pipeline
              records them. IB Flex Query stays as an optional audit overlay.
            </div>
          </div>
        )}
        {!loading && !hasData && !error && data?.configured !== false && (
          <div className="alert-item section-message">
            No historical trades. Click REFRESH to fetch from IB.
          </div>
        )}
        {!loading && pageRows.length > 0 && (
          <>
            <table>
              <thead>
                <tr>
                  <SortTh<BlotterSortKey>
                    label="Date"
                    sortKey="date"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<BlotterSortKey>
                    label="Symbol"
                    sortKey="symbol"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<BlotterSortKey>
                    label="Description"
                    sortKey="contract_desc"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<BlotterSortKey>
                    label="Type"
                    sortKey="sec_type"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<BlotterSortKey>
                    label="Side"
                    sortKey="status"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<BlotterSortKey>
                    label="Qty"
                    sortKey="net_quantity"
                    className="right"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<BlotterSortKey>
                    label="Commission"
                    sortKey="total_commission"
                    className="right"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<BlotterSortKey>
                    label="Realized P&L"
                    sortKey="realized_pnl"
                    className="right"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<BlotterSortKey>
                    label="Cost Basis"
                    sortKey="cost_basis"
                    className="right"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <SortTh<BlotterSortKey>
                    label="Proceeds"
                    sortKey="proceeds"
                    className="right"
                    activeKey={sort.key}
                    direction={sort.direction}
                    onToggle={toggle}
                  />
                  <th style={{ width: "32px" }}></th>
                </tr>
              </thead>
              <tbody>
                {pageRows.map((t, i) => (
                  <tr key={`${t.symbol}-${t.contract_desc}-${i}`}>
                    <td>
                      {getTradeDate(t)
                        ? new Date(getTradeDate(t)).toLocaleDateString()
                        : "—"}
                    </td>
                    <td>
                      <TickerLink ticker={t.symbol} />
                    </td>
                    <td>{t.contract_desc}</td>
                    <td>{t.sec_type}</td>
                    <td>
                      <span
                        className={`pill ${t.is_closed ? "neutral" : "defined"}`}
                      >
                        {t.is_closed ? "Closed" : "Open"}
                      </span>
                    </td>
                    <td className="right">
                      {t.total_quantity ?? t.net_quantity}
                    </td>
                    <td className="right">
                      {t.total_commission != null
                        ? fmtPrice(t.total_commission)
                        : "---"}
                    </td>
                    <td
                      className={`right ${(t.realized_pnl ?? 0) >= 0 ? "positive" : "negative"}`}
                    >
                      {t.realized_pnl != null ? (
                        <>
                          {t.realized_pnl >= 0 ? "+" : ""}
                          {fmtPrice(t.realized_pnl)}
                          {t.cost_basis != null && Math.abs(t.cost_basis) > 0
                            ? ` (${(t.realized_pnl / Math.abs(t.cost_basis)) * 100 >= 0 ? "+" : ""}${((t.realized_pnl / Math.abs(t.cost_basis)) * 100).toFixed(1)}%)`
                            : ""}
                        </>
                      ) : (
                        "---"
                      )}
                    </td>
                    <td className="right">
                      {t.cost_basis != null ? fmtPrice(t.cost_basis) : "---"}
                    </td>
                    <td className="right">
                      {t.proceeds != null ? fmtPrice(t.proceeds) : "---"}
                    </td>
                    <td />
                  </tr>
                ))}
              </tbody>
            </table>
            {totalPages > 1 && (
              <div className="pagination">
                <button
                  disabled={safePage === 0}
                  onClick={() => setPage(safePage - 1)}
                >
                  &larr; Prev
                </button>
                <span className="page-info">
                  Page {safePage + 1} of {totalPages}
                </span>
                <button
                  disabled={safePage >= totalPages - 1}
                  onClick={() => setPage(safePage + 1)}
                >
                  Next &rarr;
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

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
        />
      );
    case "performance":
      return (
        <PerformancePanel
          portfolioLastSync={portfolioLastSync}
          marketState={marketState}
        />
      );
    case "orders":
      return (
        <OrdersSections
          orders={orders ?? null}
          prices={prices}
          portfolio={portfolio}
        />
      );
    case "journal":
      return <JournalSections />;
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
        />
      );
  }
}
