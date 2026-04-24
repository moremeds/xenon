"use client";

/**
 * Portfolio "By Structure" view — renders one card per underlying ticker,
 * with the stock leg pinned on top and options sub-grouped by catalog
 * category. Card header totals reuse the row-level display helpers so they
 * stay in lockstep with the `PositionTable` rows beneath them.
 *
 * Contract (unified with PortfolioSections):
 *   positions      already filtered upstream by PortfolioSections
 *   prices         WS price map
 *   activeAccount  "ib" | "futu" — gates readonly rendering + collapse key
 *   lastSync       ISO string for the footer meta line
 *
 * Collapse state is ephemeral (not persisted) and namespaced by
 * `${activeAccount}:${ticker}:${category}` so switching accounts
 * renders a clean slate.
 */

import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { buildTickerGroups } from "@/lib/portfolioByStructure";
import { CATEGORY_LABELS, type CategoryKey } from "@/lib/structureCatalog";
import { fmtUsd } from "@/lib/positionUtils";
import PositionTable from "./PositionTable";

type Props = {
  positions: PortfolioPosition[];
  prices?: Record<string, PriceData>;
  activeAccount: "ib" | "futu";
  lastSync: string;
};

function fmtSigned(
  n: number | null,
  fmt: (x: number) => string = fmtUsd,
): string {
  if (n == null) return "—";
  const sign = n >= 0 ? "+" : "−";
  return `${sign}${fmt(Math.abs(n))}`;
}

function fmtPct(n: number | null): string {
  if (n == null) return "—";
  const sign = n >= 0 ? "+" : "−";
  return `${sign}${Math.abs(n).toFixed(1)}%`;
}

function fmtPrice(n: number | null): string {
  if (n == null) return "—";
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export default function PortfolioByStructure({
  positions,
  prices,
  activeAccount,
  lastSync,
}: Props) {
  const groups = useMemo(
    () =>
      buildTickerGroups(positions, prices, {
        fuseVirtualPairs: activeAccount === "futu",
      }),
    [positions, prices, activeAccount],
  );

  // Ephemeral collapse state, namespaced by activeAccount:ticker:category
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  // Reset collapse when account changes (belt + suspenders on top of namespaced keys)
  useEffect(() => {
    setCollapsed({});
  }, [activeAccount]);

  const toggleCollapse = (key: string) => {
    setCollapsed((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const readonly = activeAccount === "futu";

  if (groups.length === 0) {
    return (
      <div className="section">
        <div className="section-body">
          <div className="alert-item">
            No positions match the current filter.
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      {groups.map((group) => {
        const { ticker, stock, optionsByCategory, agg, last, dayChgPct } =
          group;
        const hasDelta = agg.netDelta != null;

        // Track whether the card has already rendered its column header row.
        // Only the first sub-table in a card shows <thead>; subsequent sub-
        // tables use hideHeader so the card has a single header line.
        let headerShown = false;
        const takeHeaderSlot = () => {
          if (headerShown) return true; // hide
          headerShown = true;
          return false; // show
        };

        return (
          <div className="section" key={ticker} data-ticker={ticker}>
            <div className="section-header">
              <div className="section-title">
                <span style={{ fontWeight: 600 }}>{ticker}</span>
                <span className="cell-muted">
                  {fmtPrice(last)}{" "}
                  {dayChgPct != null ? `(${fmtPct(dayChgPct)})` : ""}
                </span>
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "12px",
                  flexWrap: "wrap",
                }}
              >
                <span className="cell-muted">
                  MV {agg.mv != null ? fmtUsd(agg.mv) : "—"}
                </span>
                <span
                  className={
                    agg.dayPnl != null
                      ? agg.dayPnl >= 0
                        ? "positive"
                        : "negative"
                      : "cell-muted"
                  }
                >
                  Day {fmtSigned(agg.dayPnl)}
                </span>
                <span
                  className={
                    agg.totalPnl != null
                      ? agg.totalPnl >= 0
                        ? "positive"
                        : "negative"
                      : "cell-muted"
                  }
                >
                  P&amp;L {fmtSigned(agg.totalPnl)}
                  {agg.totalPnlPct != null
                    ? ` (${fmtPct(agg.totalPnlPct)})`
                    : ""}
                </span>
                <span
                  className={`pill ${hasDelta ? "neutral" : "neutral"}`}
                  aria-label="net delta"
                >
                  Δ {hasDelta ? agg.netDelta!.toFixed(0) : "—"}
                </span>
              </div>
            </div>
            <div className="section-body">
              {stock && (
                <PositionTable
                  positions={[stock]}
                  showExpiry={true}
                  showUnderlying={true}
                  prices={prices}
                  readonly={readonly}
                  hideHeader={takeHeaderSlot()}
                />
              )}
              {Array.from(optionsByCategory.entries()).map(
                ([category, rows]) => {
                  const key = `${activeAccount}:${ticker}:${category}`;
                  const isCollapsed = Boolean(collapsed[key]);
                  const label = CATEGORY_LABELS[category as CategoryKey];

                  // Sub-group rows by virtual-pair key. Unpaired positions get
                  // a synthetic key so they render in their own sub-block.
                  const subGroups: Array<{
                    pairKey: string;
                    label: string | null;
                    positions: PortfolioPosition[];
                  }> = [];
                  const byPair = new Map<
                    string,
                    { label: string | null; positions: PortfolioPosition[] }
                  >();
                  for (const row of rows) {
                    const pair = group.virtualPairs.get(row.id);
                    const pairKey = pair?.pairKey ?? `solo-${row.id}`;
                    let entry = byPair.get(pairKey);
                    if (!entry) {
                      entry = { label: pair?.label ?? null, positions: [] };
                      byPair.set(pairKey, entry);
                      subGroups.push({
                        pairKey,
                        label: entry.label,
                        positions: entry.positions,
                      });
                    }
                    entry.positions.push(row);
                  }

                  return (
                    <div key={category} data-category={category}>
                      <button
                        type="button"
                        className="section-title"
                        onClick={() => toggleCollapse(key)}
                        aria-expanded={!isCollapsed}
                        aria-controls={`group-${ticker}-${category}`}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "6px",
                          background: "none",
                          border: "none",
                          padding: "8px 0",
                          cursor: "pointer",
                          width: "100%",
                          textAlign: "left",
                        }}
                      >
                        {isCollapsed ? (
                          <ChevronRight size={12} />
                        ) : (
                          <ChevronDown size={12} />
                        )}
                        <span
                          style={{
                            fontSize: "11px",
                            textTransform: "uppercase",
                            letterSpacing: "0.05em",
                          }}
                        >
                          {label}
                        </span>
                        <span
                          className="cell-muted"
                          style={{ fontSize: "11px" }}
                        >
                          {rows.length}
                        </span>
                      </button>
                      {!isCollapsed && (
                        <div id={`group-${ticker}-${category}`}>
                          {subGroups.map((sg) => {
                            // When fusion is on, a virtual pair appears in `sg.positions` as a single
                            // multi-leg PortfolioPosition — the combo row already displays the pair
                            // label, so the standalone text header above would be redundant.
                            const isFusedCombo =
                              sg.positions.length === 1 &&
                              sg.positions[0].legs.length === 2 &&
                              group.virtualPairs.has(sg.positions[0].id);
                            const showLabel = sg.label && !isFusedCombo;
                            return (
                              <div key={sg.pairKey} data-pair-key={sg.pairKey}>
                                {showLabel ? (
                                  <div
                                    className="cell-muted"
                                    style={{
                                      fontSize: "11px",
                                      padding: "6px 0 2px 18px",
                                      letterSpacing: "0.02em",
                                    }}
                                  >
                                    {sg.label}
                                  </div>
                                ) : null}
                                <PositionTable
                                  positions={sg.positions}
                                  showUnderlying={true}
                                  prices={prices}
                                  readonly={readonly}
                                  hideHeader={takeHeaderSlot()}
                                />
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                },
              )}
            </div>
          </div>
        );
      })}

      <div className="section">
        <div className="report-meta">
          Last Sync: {new Date(lastSync).toLocaleString()} • Source:{" "}
          {activeAccount === "futu" ? "Futu OpenD" : "IB Gateway"}
        </div>
      </div>
    </>
  );
}
