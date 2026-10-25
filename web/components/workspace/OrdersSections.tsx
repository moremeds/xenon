"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  Loader2,
  XCircle,
} from "lucide-react";
import type {
  ExecutedOrder,
  OpenOrder,
  OrdersData,
  PortfolioData,
} from "@/lib/types";
import { useOrderActions } from "@/lib/OrderActionsContext";
import type { PriceData } from "@/lib/pricesProtocol";
import { useSort } from "@/lib/useSort";
import { useTableFilter } from "@/lib/useTableFilter";
import TableSearch from "../TableSearch";
import { fmtPrice } from "@/lib/positionUtils";
import {
  buildOpenOrderDisplayRows,
  type OpenOrderDisplayRow,
  resolveOpenOrderComboPrice,
} from "@/lib/openOrderCombos";
import { buildGroupedComboModifyTarget } from "@/lib/openOrderComboModify";
import CancelOrderDialog from "../CancelOrderDialog";
import ModifyOrderModal from "../ModifyOrderModal";
import type { ModifyOrderRequest } from "@/lib/orderModify";
import InfoTooltip from "../InfoTooltip";
import { SECTION_TOOLTIPS } from "@/lib/sectionTooltips";
import TickerLink from "../TickerLink";
import { formatEtTime } from "@/lib/timeFormat";
import { SortTh } from "./shared";
import {
  execOrderExtract,
  makeOpenOrderExtract,
  OrderPriceCell,
  resolveOrderLastPrice,
  type ExecOrderKey,
  type OpenOrderKey,
} from "./orderTable";
import {
  executedGroupDetailFills,
  groupExecutedOrders,
  type PositionFillGroup,
} from "./fillGrouping";
import { HistoricalTradesSection } from "./HistoricalTradesSection";
import {
  FUTU_MANAGEMENT_UNCONFIRMED_COPY,
  futuManageableOpenOrder,
  futuModifiableOpenOrder,
  isFutuOpenOrder,
} from "@/lib/futuOpenOrder";
import { useFutuAccountEnv } from "@/lib/futuAccountEnv";
import {
  orderActionScope,
  scopesMatch,
  type OrderActionScope,
} from "@/lib/orderActionScope";
import {
  futuCapabilitiesForEnv,
  futuTradingEnabledForEnv,
  useFutuTrading,
} from "@/lib/useFutuTrading";

export function OrdersSections({
  orders,
  prices,
  portfolio,
  activeAccount = "ib",
  futuOrderActionsEnabled = false,
}: {
  orders: OrdersData | null;
  prices?: Record<string, PriceData>;
  portfolio?: PortfolioData | null;
  activeAccount?: "ib" | "futu";
  futuOrderActionsEnabled?: boolean;
}) {
  // Futu is read-only: no order modify/cancel surfaces (mirrors the position table gate).
  const readonly = activeAccount === "futu" && !futuOrderActionsEnabled;
  const futuEnv = useFutuAccountEnv();
  const futuTrading = useFutuTrading();
  const viewScope: OrderActionScope = useMemo(
    () =>
      activeAccount === "futu"
        ? { broker: "FUTU", accountEnv: futuEnv }
        : { broker: "IB" },
    [activeAccount, futuEnv],
  );
  const {
    isPendingCancel: checkPendingCancel,
    isPendingModify: checkPendingModify,
    cancelledOrders,
    requestCancel,
    requestModify,
    lockedReplacements,
    isFutuManagementLocked,
  } = useOrderActions();
  const futuComboActionsHidden =
    activeAccount === "futu" && futuOrderActionsEnabled;
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

  useEffect(() => {
    setCancelTarget(null);
    setModifyTarget(null);
  }, [viewScope.broker, viewScope.accountEnv, futuOrderActionsEnabled]);

  const canManageOrder = useCallback(
    (o: OpenOrder) => {
      if (!scopesMatch(orderActionScope(o, futuEnv), viewScope)) return false;
      if (isFutuOpenOrder(o)) {
        if (!futuManageableOpenOrder(o)) return false;
        if (isFutuManagementLocked(o)) return false;
        if (
          !futuTradingEnabledForEnv(
            futuTrading,
            o.accountEnv === "paper" || o.accountEnv === "live"
              ? o.accountEnv
              : futuEnv,
          )
        ) {
          return false;
        }
      }
      return true;
    },
    [futuEnv, viewScope, futuTrading, isFutuManagementLocked],
  );

  /** Modify: Futu only for types in `capabilities[env].modify` (cancel
   *  stays available for every type); IB LMT / STP LMT. */
  const canModifyOrder = useCallback(
    (o: OpenOrder) => {
      if (!canManageOrder(o)) return false;
      if (isFutuOpenOrder(o)) {
        const env =
          o.accountEnv === "paper" || o.accountEnv === "live"
            ? o.accountEnv
            : futuEnv;
        return futuModifiableOpenOrder(
          o,
          futuCapabilitiesForEnv(futuTrading, env),
        );
      }
      return o.orderType === "LMT" || o.orderType === "STP LMT";
    },
    [canManageOrder, futuEnv, futuTrading],
  );

  const handleCancel = useCallback(async () => {
    if (!cancelTarget || !canManageOrder(cancelTarget)) {
      setCancelTarget(null);
      return;
    }
    setActionLoading(true);
    const result = await requestCancel(cancelTarget);
    setActionLoading(false);
    // B4 — keep modal open on failure so the user sees the FAILED pill on the
    // row and the toast. Dismiss on a clean 2xx, or when the outcome is
    // uncertain (the row then shows the unconfirmed state, no resend).
    if (result.ok || isFutuManagementLocked(cancelTarget)) {
      setCancelTarget(null);
    }
  }, [cancelTarget, requestCancel, canManageOrder, isFutuManagementLocked]);

  const handleModify = useCallback(
    async (request: ModifyOrderRequest) => {
      if (!modifyTarget || !canModifyOrder(modifyTarget.requestOrder)) {
        setModifyTarget(null);
        return;
      }
      setActionLoading(true);
      const result = await requestModify(
        modifyTarget.requestOrder,
        modifyTarget.cancelOrders?.length
          ? { ...request, cancelOrders: modifyTarget.cancelOrders }
          : request,
      );
      setActionLoading(false);
      if (result.ok || isFutuManagementLocked(modifyTarget.requestOrder)) {
        setModifyTarget(null);
      }
    },
    [modifyTarget, requestModify, canModifyOrder, isFutuManagementLocked],
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

  const canModify = canModifyOrder;
  const execCount = orders.executed_count + cancelledOrders.length;

  return (
    <>
      <CancelOrderDialog
        order={
          cancelTarget && canManageOrder(cancelTarget) ? cancelTarget : null
        }
        loading={actionLoading}
        onConfirm={handleCancel}
        onClose={() => setCancelTarget(null)}
      />
      <ModifyOrderModal
        order={
          modifyTarget && canModifyOrder(modifyTarget.requestOrder)
            ? modifyTarget.modalOrder
            : null
        }
        loading={actionLoading}
        prices={prices}
        portfolio={portfolio}
        onConfirm={handleModify}
        onClose={() => setModifyTarget(null)}
        locked={
          modifyTarget != null &&
          Boolean(lockedReplacements?.has(modifyTarget.requestOrder.permId))
        }
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
                    const comboPendingCancel = o.orders.some((order) =>
                      checkPendingCancel(order),
                    );
                    const comboPendingModify = o.orders.some((order) =>
                      checkPendingModify(order),
                    );
                    const isPending = comboPendingCancel || comboPendingModify;

                    return (
                      <tr
                        key={o.id}
                        className={
                          comboPendingCancel
                            ? "row-pending-cancel"
                            : comboPendingModify
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
                              comboPendingModify ? "status-modifying" : ""
                            }
                          >
                            {comboPendingModify
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
                          {comboPendingCancel ? (
                            <span className="status-cancelling">
                              Cancelling...
                            </span>
                          ) : comboPendingModify ? (
                            <span className="status-modifying">
                              Modifying...
                            </span>
                          ) : (
                            o.status
                          )}
                        </td>
                        <td>{o.tif}</td>
                        <td className="actions-cell">
                          {futuComboActionsHidden ? null : readonly ? (
                            <span className="cancel-pending-label">
                              READ-ONLY
                            </span>
                          ) : isPending ? (
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

                  const rowPendingCancel = checkPendingCancel(o.order);
                  const rowPendingModify = checkPendingModify(o.order);
                  const isPending = rowPendingCancel || rowPendingModify;
                  const rowKey =
                    o.order.brokerOrderId ??
                    o.order.submissionId ??
                    `${o.order.orderId}-${o.order.permId}`;
                  return (
                    <tr
                      key={rowKey}
                      className={
                        rowPendingCancel
                          ? "row-pending-cancel"
                          : rowPendingModify
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
                        {rowPendingModify && o.order.orderType === "STP LMT" ? (
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
                        {rowPendingCancel ? (
                          <span className="status-cancelling">
                            Cancelling...
                          </span>
                        ) : rowPendingModify ? (
                          <span className="status-modifying">Modifying...</span>
                        ) : (
                          o.order.status
                        )}
                      </td>
                      <td>{o.order.tif}</td>
                      <td className="actions-cell">
                        {readonly ? (
                          <span className="cancel-pending-label">
                            READ-ONLY
                          </span>
                        ) : isPending ? (
                          <span className="cancel-pending-label">PENDING</span>
                        ) : isFutuManagementLocked(o.order) ? (
                          <span className="cancel-pending-label">
                            {FUTU_MANAGEMENT_UNCONFIRMED_COPY}
                          </span>
                        ) : (
                          <>
                            <button
                              className="btn-order-action btn-modify"
                              disabled={!canModify(o.order)}
                              title={
                                canModify(o.order)
                                  ? "Modify order"
                                  : isFutuOpenOrder(o.order)
                                    ? `Modify of ${o.order.orderType} is off for this Futu environment; cancel instead`
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
                        <td>{formatEtTime(group.time)}</td>
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
                                {formatEtTime(e.time)}
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

      <HistoricalTradesSection activeAccount={activeAccount} />
    </>
  );
}
