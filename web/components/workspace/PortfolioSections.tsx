"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, Circle, TriangleAlert } from "lucide-react";
import type { PortfolioData, PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { useTableFilter } from "@/lib/useTableFilter";
import TableSearch from "../TableSearch";
import PortfolioByStructure from "../PortfolioByStructure";
import PositionTable from "../PositionTable";
import InfoTooltip from "../InfoTooltip";
import { SECTION_TOOLTIPS } from "@/lib/sectionTooltips";
import type { FutuInitialRead } from "@/lib/useFutuPortfolio";

type PortfolioViewMode = "risk" | "structure";
const PORTFOLIO_VIEW_KEY = "xenon.portfolio.view";

export function PortfolioSections({
  portfolio,
  prices,
  activeAccount = "ib",
  futuRead,
  onFutuRetry,
  futuRetrying = false,
}: {
  portfolio: PortfolioData | null;
  prices?: Record<string, PriceData>;
  activeAccount?: "ib" | "futu";
  /** Futu tab only: result of the initial /api/futu/portfolio read. */
  futuRead?: FutuInitialRead;
  /** Futu tab only: retry action wired to useFutuPortfolio().syncNow(). */
  onFutuRetry?: () => void;
  futuRetrying?: boolean;
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
    // Futu tab: the initial read finished with a definitive non-data
    // outcome — describe the measurement condition instead of spinning
    // LOADING forever (nothing more arrives until the user syncs).
    const futuEmptyState: "never_synced" | "unavailable" | null =
      activeAccount === "futu" &&
      (futuRead === "never_synced" || futuRead === "unavailable")
        ? futuRead
        : null;
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
            <span className="pill neutral">
              {futuEmptyState === "never_synced"
                ? "NEVER SYNCED"
                : futuEmptyState === "unavailable"
                  ? "UNAVAILABLE"
                  : "LOADING"}
            </span>
          </div>
          <div className="section-body">
            {futuEmptyState ? (
              <>
                <div className="alert-item" data-testid="futu-empty-state">
                  {futuEmptyState === "never_synced"
                    ? "Futu not yet synced — start Futu OpenD, then sync."
                    : "Futu unavailable — FastAPI did not return a snapshot."}
                </div>
                {onFutuRetry ? (
                  <button
                    type="button"
                    className="sync-button"
                    onClick={onFutuRetry}
                    disabled={futuRetrying}
                    data-testid="futu-retry"
                  >
                    {futuRetrying ? "Syncing..." : "Retry sync"}
                  </button>
                ) : null}
              </>
            ) : (
              <div className="alert-item">Waiting for portfolio data...</div>
            )}
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
          fxRates={portfolio.fx_rates}
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
              fxRates={portfolio.fx_rates}
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
              fxRates={portfolio.fx_rates}
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
              fxRates={portfolio.fx_rates}
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
