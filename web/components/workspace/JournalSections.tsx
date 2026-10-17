"use client";

import React, { useCallback, useMemo, useState } from "react";
import { ChevronDown, Wrench } from "lucide-react";
import type { TradeEntry } from "@/lib/types";
import { useJournal } from "@/lib/useJournal";
import { useSort } from "@/lib/useSort";
import { useTableFilter } from "@/lib/useTableFilter";
import TableSearch from "../TableSearch";
import { TableSkeleton } from "@/components/ui/Skeleton";
import TickerLink from "../TickerLink";
import InfoTooltip from "../InfoTooltip";
import { SECTION_TOOLTIPS } from "@/lib/sectionTooltips";
import { SortTh } from "./shared";

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

export const JournalSections = React.memo(function JournalSections({
  activeAccount = "ib",
}: {
  activeAccount?: "ib" | "futu";
}) {
  const { data, loading, error, syncWithIB, syncing, lastSyncResult } =
    useJournal(true, activeAccount === "futu" ? "FUTU" : "IB");
  const [syncError, setSyncError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const trades = useMemo(() => {
    if (!data?.trades) return [];
    // Most-recent first by trade date (id is no longer a recency proxy once
    // Futu auto-imports are rebuilt/grouped); id breaks same-day ties.
    return [...data.trades].sort(
      (a, b) => b.date.localeCompare(a.date) || b.id - a.id,
    );
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
  } = useSort(filtered, journalSortExtract, "date" as JournalSortKey, "desc");

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
            {activeAccount !== "futu" && (
              <button
                className="btn-sync"
                onClick={handleSync}
                disabled={syncing}
                title="Sync unreconciled IB trades into journal"
              >
                {syncing ? "SYNCING..." : "SYNC IB"}
              </button>
            )}
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
            <table className="journal-table">
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
                        <td className="journal-date">{t.date}</td>
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
