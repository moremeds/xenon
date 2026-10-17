"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ClipboardList, Loader2 } from "lucide-react";
import type { BlotterTrade } from "@/lib/types";
import { SourcePill } from "../SourcePill";
import { useBlotter } from "@/lib/useBlotter";
import { useSort } from "@/lib/useSort";
import { useTableFilter } from "@/lib/useTableFilter";
import TableSearch from "../TableSearch";
import { fmtPrice } from "@/lib/positionUtils";
import { TableSkeleton } from "@/components/ui/Skeleton";
import InfoTooltip from "../InfoTooltip";
import { SECTION_TOOLTIPS } from "@/lib/sectionTooltips";
import TickerLink from "../TickerLink";
import { SortTh } from "./shared";

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

export function HistoricalTradesSection({
  activeAccount = "ib",
}: {
  activeAccount?: "ib" | "futu";
}) {
  const { data, loading, syncing, error, syncNow } = useBlotter(
    true,
    activeAccount === "futu" ? "FUTU" : "IB",
  );
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
        {!loading && !error && data?.flex_error && (
          <div className="alert-item section-message">
            <div>Flex sync failed: {data.flex_error}</div>
            {data?.message ? (
              <div
                style={{
                  marginTop: "0.5rem",
                  opacity: 0.7,
                  fontSize: "0.85em",
                }}
              >
                {data.message}
              </div>
            ) : null}
          </div>
        )}
        {!loading &&
          !hasData &&
          !error &&
          data?.configured !== false &&
          !data?.flex_error && (
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
