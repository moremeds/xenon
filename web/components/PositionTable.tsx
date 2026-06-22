"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ChevronDown, ChevronUp, Zap } from "lucide-react";
import type { PortfolioLeg, PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import InstrumentDetailModal from "./InstrumentDetailModal";
import PositionOrderModal from "./PositionOrderModal";
import { useSort, type SortDirection } from "@/lib/useSort";
import TickerLink from "./TickerLink";
import {
  fmtUsd,
  fmtPrice,
  fmtPriceOrCalculated,
  resolveMarketValue,
  resolveEntryCost,
  getAvgEntry,
  getMultiplier,
  legMultiplier,
  getLastPrice,
  getLastPriceIsCalculated,
  legPriceKey,
  getOptionDailyChg,
  getTodayPnlDollars,
  resolveRealtimePrice,
  nativeToDisplayUsd,
} from "@/lib/positionUtils";
import { fmtNative } from "@/lib/fx";
import { useFx } from "@/lib/useFx";
import FxBadge from "./FxBadge";

/* ─── Sortable header cell ─────────────────────────────── */

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

/* ─── Price direction hook ─────────────────────────────── */

export function usePriceDirection(price: number | null): {
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

/* ─── Helpers ──────────────────────────────────────────── */

function getDailyChange(realtimePrice?: PriceData | null): number | null {
  if (!realtimePrice) return null;
  const { last, close } = realtimePrice;
  if (last == null || last <= 0 || close == null || close <= 0) return null;
  return ((last - close) / close) * 100;
}

function getOptionRtMv(
  pos: PortfolioPosition,
  prices?: Record<string, PriceData>,
): number | null {
  if (pos.structure_type === "Stock") return null;
  let rtMv = 0;
  for (const leg of pos.legs) {
    const key = legPriceKey(pos.ticker, pos.expiry, leg);
    const lp = key && prices ? prices[key] : null;
    const current = resolveRealtimePrice(
      lp,
      leg.market_price,
      Boolean(leg.market_price_is_calculated),
    ).price;
    if (current == null) return null;
    const sign = leg.direction === "LONG" ? 1 : -1;
    rtMv += sign * current * leg.contracts * legMultiplier(leg);
  }
  return rtMv;
}

/* ─── Sort extract factory ─────────────────────────────── */

export type PositionSortKey =
  | "ticker"
  | "structure"
  | "qty"
  | "direction"
  | "underlying"
  | "avg_entry"
  | "last_price"
  | "daily_chg"
  | "today_pnl"
  | "entry_cost"
  | "market_value"
  | "pnl"
  | "expiry";

function makePositionExtract(
  prices?: Record<string, PriceData>,
  usdPerUnit: Record<string, number> = { USD: 1 },
) {
  return (
    pos: PortfolioPosition,
    key: PositionSortKey,
  ): string | number | null => {
    const isStock = pos.structure_type === "Stock";
    const _stockLast = prices?.[pos.ticker]?.last;
    const rtStockLast =
      _stockLast != null && _stockLast > 0 ? _stockLast : null;
    const optRtMv = getOptionRtMv(pos, prices);
    const mv =
      isStock && rtStockLast != null
        ? rtStockLast * pos.contracts
        : (optRtMv ?? resolveMarketValue(pos));
    // Money sort keys must be in ONE unit (USD) or a ¥/₩ row floats to the top
    // by raw magnitude. Convert native → USD for non-USD rows (USD passthrough).
    const mvUsd = nativeToDisplayUsd(
      mv,
      pos.currency,
      usdPerUnit,
      pos.market_value_usd,
    );
    const entryUsd = nativeToDisplayUsd(
      resolveEntryCost(pos),
      pos.currency,
      usdPerUnit,
      pos.entry_cost_usd,
    );
    switch (key) {
      case "ticker":
        return pos.ticker;
      case "structure":
        return pos.structure;
      case "qty":
        return pos.contracts;
      case "direction":
        return pos.direction;
      case "underlying":
        return rtStockLast;
      case "avg_entry":
        return getAvgEntry(pos);
      case "last_price": {
        if (isStock && rtStockLast != null) return rtStockLast;
        if (optRtMv != null)
          return optRtMv / (pos.contracts * getMultiplier(pos));
        return getLastPrice(pos);
      }
      case "daily_chg":
        return isStock
          ? getDailyChange(prices?.[pos.ticker])
          : getOptionDailyChg(pos, prices);
      case "today_pnl":
        return getTodayPnlDollars(pos, prices);
      case "entry_cost":
        return entryUsd;
      case "market_value":
        return mvUsd;
      case "pnl":
        return mvUsd != null && entryUsd != null ? mvUsd - entryUsd : null;
      case "expiry":
        return pos.expiry === "N/A" ? null : pos.expiry;
      default:
        return null;
    }
  };
}

/* ─── Leg row ──────────────────────────────────────────── */

function LegRow({
  leg,
  showExpiry,
  showUnderlying,
  realtimeLegPrice,
  onLegClick,
}: {
  leg: PortfolioPosition["legs"][number];
  showExpiry: boolean;
  showUnderlying?: boolean;
  realtimeLegPrice?: PriceData | null;
  onLegClick?: (leg: PortfolioLeg) => void;
}) {
  const resolvedPrice = resolveRealtimePrice(
    realtimeLegPrice,
    leg.market_price != null ? Math.abs(leg.market_price) : null,
    Boolean(leg.market_price_is_calculated),
  );
  const marketPrice = resolvedPrice.price;
  const isCalculated = resolvedPrice.isCalculated;
  const { direction: priceDirection, flashDirection } =
    usePriceDirection(marketPrice);

  // Per-leg P&L: sign-aware (MV - EC)
  const mult = legMultiplier(leg);
  const legMv =
    marketPrice != null
      ? marketPrice * leg.contracts * mult
      : leg.market_value != null
        ? Math.abs(leg.market_value)
        : null;
  const legEc = Math.abs(leg.entry_cost);
  const sign = leg.direction === "LONG" ? 1 : -1;
  const legPnl = legMv != null ? sign * (legMv - legEc) : null;

  return (
    <tr className={flashDirection ? `last-price-${flashDirection}` : undefined}>
      <td></td>
      <td
        colSpan={3}
        className={`cell-indent cell-muted ${onLegClick ? "leg-clickable" : ""}`}
        onClick={onLegClick ? () => onLegClick(leg) : undefined}
      >
        {leg.direction} {leg.contracts}x {leg.type}
        {leg.strike ? ` $${leg.strike}` : ""}
      </td>
      {showUnderlying && <td></td>}
      <td className="right cell-muted">
        {fmtPrice(Math.abs(leg.avg_cost) / (leg.type === "Stock" ? 1 : 100))}
      </td>
      <td className="right last-price-cell">
        {marketPrice != null
          ? fmtPriceOrCalculated(marketPrice, isCalculated)
          : "—"}
        {priceDirection === "up" && (
          <ArrowUp
            size={11}
            className="price-trend-icon price-trend-up"
            aria-label="price up"
          />
        )}
        {priceDirection === "down" && (
          <ArrowDown
            size={11}
            className="price-trend-icon price-trend-down"
            aria-label="price down"
          />
        )}
      </td>
      <td></td>
      <td></td>
      <td className="right cell-muted">{fmtPrice(legEc)}</td>
      <td className="right cell-muted">
        {legMv != null ? fmtUsd(legMv) : "—"}
      </td>
      <td
        className={`right cell-muted ${legPnl != null ? (legPnl >= 0 ? "positive" : "negative") : ""}`}
      >
        {legPnl != null
          ? `${legPnl >= 0 ? "+" : "-"}${fmtUsd(Math.abs(legPnl))}`
          : "—"}
      </td>
      {showExpiry && <td></td>}
    </tr>
  );
}

/* ─── Position row ─────────────────────────────────────── */

function PositionRow({
  pos,
  showExpiry = true,
  showUnderlying = false,
  realtimePrice,
  prices,
  usdPerUnit = { USD: 1 },
  onLegClick,
  onOrderClick,
  readonly = false,
}: {
  pos: PortfolioPosition;
  showExpiry?: boolean;
  showUnderlying?: boolean;
  realtimePrice?: PriceData | null;
  prices?: Record<string, PriceData>;
  usdPerUnit?: Record<string, number>;
  onLegClick?: (leg: PortfolioLeg, pos: PortfolioPosition) => void;
  onOrderClick?: (pos: PortfolioPosition) => void;
  readonly?: boolean;
}) {
  const [legsExpanded, setLegsExpanded] = useState(false);
  const hasMultipleLegs = pos.legs.length > 1;

  // For stock positions, prefer the real-time WS price over the stale sync price
  const isStock = pos.structure_type === "Stock";
  const rtLast =
    isStock && realtimePrice?.last != null && realtimePrice.last > 0
      ? realtimePrice.last
      : null;

  // For options: compute real-time MV and daily change from leg-level WS prices
  const optionsRt = useMemo(() => {
    if (isStock) return null;
    let rtMv = 0;
    let rtDailyPnl = 0;
    let rtCloseValue = 0;
    let hasCloseData = false;
    let priceIsCalculated = false;
    for (const leg of pos.legs) {
      const key = legPriceKey(pos.ticker, pos.expiry, leg);
      const lp = key && prices ? prices[key] : null;
      const resolved = resolveRealtimePrice(
        lp,
        leg.market_price,
        Boolean(leg.market_price_is_calculated),
      );
      const current = resolved.price;
      if (current == null) return null;
      priceIsCalculated = priceIsCalculated || resolved.isCalculated;
      const sign = leg.direction === "LONG" ? 1 : -1;
      const mult = legMultiplier(leg);
      rtMv += sign * current * leg.contracts * mult;
      const close = lp?.close;
      if (close != null && close > 0) {
        rtDailyPnl += sign * (current - close) * leg.contracts * mult;
        rtCloseValue += sign * close * leg.contracts * mult;
        hasCloseData = true;
      }
    }
    return {
      mv: rtMv,
      dailyPnl: hasCloseData ? rtDailyPnl : null,
      closeValue: rtCloseValue,
      priceIsCalculated,
    };
  }, [isStock, prices, pos.legs, pos.ticker, pos.expiry]);

  const mv =
    rtLast != null
      ? rtLast * pos.contracts
      : (optionsRt?.mv ?? resolveMarketValue(pos));
  const entryCost = resolveEntryCost(pos);
  const pnl = mv != null ? mv - entryCost : null;
  const pnlPct =
    pnl != null && entryCost !== 0 ? (pnl / Math.abs(entryCost)) * 100 : null;
  const avgEntry = getAvgEntry(pos);
  const lastPrice =
    rtLast ??
    (optionsRt
      ? mv! / (pos.contracts * getMultiplier(pos))
      : getLastPrice(pos));
  const lastPriceIsCalculated =
    rtLast != null
      ? false
      : optionsRt
        ? optionsRt.priceIsCalculated
        : getLastPriceIsCalculated(pos);
  const { direction: priceDirection, flashDirection } =
    usePriceDirection(lastPrice);
  // Stock: daily change from underlying WS price
  // Options: prefer IB's per-position daily P&L (handles intraday additions correctly)
  //          then fall back to WS close-based calculation
  const wsDailyPnl = optionsRt?.dailyPnl ?? null;
  const wsCloseValue = optionsRt?.closeValue ?? 0;
  // IB's reqPnLSingle daily P&L — correctly handles blended positions
  // (overnight contracts use yesterday's close, intraday adds use fill price)
  const ibDailyPnl =
    !isStock && pos.ib_daily_pnl != null ? pos.ib_daily_pnl : null;
  const effectiveDailyPnl = ibDailyPnl ?? wsDailyPnl;

  // Same-day positions opened today: yesterday's close is meaningless.
  // Use entry-cost-based P&L instead (Today's P&L = Total P&L).
  const dailyChg = isStock
    ? getDailyChange(realtimePrice)
    : getOptionDailyChg(pos, prices);

  // Today's P&L in dollars
  const todayPnl = isStock
    ? realtimePrice?.last != null &&
      realtimePrice.last > 0 &&
      realtimePrice?.close != null &&
      realtimePrice.close > 0
      ? (realtimePrice.last - realtimePrice.close) * pos.contracts
      : null
    : getTodayPnlDollars(pos, prices);

  // ── Multi-currency display (Japan/Korea) ──
  // IB returns mv/entryCost/pnl/todayPnl in the contract's NATIVE currency.
  // For a non-USD row, convert money columns to a USD headline (with a native
  // sub-line on MV); per-share price columns (avg entry, last) stay native.
  // USD rows: cur === "USD" so every *Usd value === its native input.
  const cur = (pos.currency || "USD").toUpperCase();
  const isForeign = cur !== "USD";
  const mvUsd = nativeToDisplayUsd(mv, cur, usdPerUnit, pos.market_value_usd);
  const entryCostUsd = nativeToDisplayUsd(
    entryCost,
    cur,
    usdPerUnit,
    pos.entry_cost_usd,
  );
  const pnlUsd =
    mvUsd != null && entryCostUsd != null ? mvUsd - entryCostUsd : null;
  const pnlPctUsd =
    pnlUsd != null && entryCostUsd != null && entryCostUsd !== 0
      ? (pnlUsd / Math.abs(entryCostUsd)) * 100
      : null;
  const todayPnlUsd = nativeToDisplayUsd(todayPnl, cur, usdPerUnit);

  // Structure already includes strike from ib_sync format_structure_description()
  const structureDisplay = pos.structure;

  // Underlying price (for options positions)
  const underlyingPrice =
    realtimePrice?.last != null && realtimePrice.last !== 0
      ? realtimePrice.last
      : null;
  const { direction: underlyingDirection, flashDirection: underlyingFlash } =
    usePriceDirection(underlyingPrice);

  return (
    <>
      <tr
        className={flashDirection ? `last-price-${flashDirection}` : undefined}
      >
        <td>
          {hasMultipleLegs ? (
            <span className="ticker-with-chevron">
              <TickerLink
                ticker={pos.ticker}
                positionId={pos.id}
                disabled={readonly}
              />
              {!readonly && onOrderClick && (
                <button
                  type="button"
                  className="position-order-btn"
                  aria-label={`Create order for ${pos.ticker} position`}
                  onClick={() => onOrderClick(pos)}
                >
                  <Zap size={12} />
                </button>
              )}
              <button
                className="leg-toggle-btn"
                onClick={() => setLegsExpanded((v) => !v)}
                aria-expanded={legsExpanded}
                aria-label={`${legsExpanded ? "Collapse" : "Expand"} legs for ${pos.ticker}`}
              >
                {legsExpanded ? (
                  <ChevronUp size={12} />
                ) : (
                  <ChevronDown size={12} />
                )}
              </button>
            </span>
          ) : (
            <span className="ticker-with-chevron">
              <TickerLink
                ticker={pos.ticker}
                positionId={pos.id}
                disabled={readonly}
              />
              {!readonly && onOrderClick && (
                <button
                  type="button"
                  className="position-order-btn"
                  aria-label={`Create order for ${pos.ticker} position`}
                  onClick={() => onOrderClick(pos)}
                >
                  <Zap size={12} />
                </button>
              )}
            </span>
          )}
        </td>
        <td>{structureDisplay}</td>
        <td className="right">
          {Number.isInteger(pos.contracts)
            ? pos.contracts
            : pos.contracts.toFixed(2)}
        </td>
        <td>
          <span
            className={`pill ${pos.risk_profile === "defined" ? "defined" : pos.risk_profile === "equity" ? "neutral" : "undefined"}`}
          >
            {pos.direction}
          </span>
        </td>
        {showUnderlying && (
          <td
            className={`right last-price-cell ${underlyingFlash ? `last-price-${underlyingFlash}` : ""}`}
          >
            {underlyingPrice != null ? fmtPrice(underlyingPrice) : "—"}
            {underlyingDirection === "up" && (
              <ArrowUp
                size={11}
                className="price-trend-icon price-trend-up"
                aria-label="underlying up"
              />
            )}
            {underlyingDirection === "down" && (
              <ArrowDown
                size={11}
                className="price-trend-icon price-trend-down"
                aria-label="underlying down"
              />
            )}
          </td>
        )}
        <td className="right">
          {isForeign ? fmtNative(avgEntry, cur) : fmtPrice(avgEntry)}
        </td>
        <td
          className={`right last-price-cell ${flashDirection ? `last-price-${flashDirection}` : ""}`}
        >
          {lastPrice != null
            ? isForeign
              ? fmtNative(lastPrice, cur)
              : fmtPriceOrCalculated(lastPrice, lastPriceIsCalculated)
            : "—"}
          {priceDirection === "up" && (
            <ArrowUp
              size={11}
              className="price-trend-icon price-trend-up"
              aria-label="price up"
            />
          )}
          {priceDirection === "down" && (
            <ArrowDown
              size={11}
              className="price-trend-icon price-trend-down"
              aria-label="price down"
            />
          )}
        </td>
        <td
          className={`right ${dailyChg != null ? (dailyChg >= 0 ? "positive" : "negative") : ""}`}
        >
          {dailyChg != null
            ? `${dailyChg >= 0 ? "+" : ""}${dailyChg.toFixed(2)}%`
            : "—"}
        </td>
        <td
          className={`right ${todayPnlUsd != null ? (todayPnlUsd >= 0 ? "positive" : "negative") : ""}`}
        >
          {todayPnlUsd != null
            ? `${todayPnlUsd >= 0 ? "+" : "-"}${fmtUsd(Math.abs(todayPnlUsd))}`
            : "—"}
        </td>
        <td className="right">
          {entryCostUsd != null ? fmtUsd(entryCostUsd) : "—"}
        </td>
        <td className="right">
          {mvUsd != null ? fmtUsd(mvUsd) : "—"}
          {isForeign && mv != null && (
            <div className="fx-native-subline">{fmtNative(mv, cur)}</div>
          )}
        </td>
        <td
          className={`right ${pnlUsd != null ? (pnlUsd >= 0 ? "positive" : "negative") : ""}`}
        >
          {pnlUsd != null
            ? `${pnlUsd >= 0 ? "+" : "-"}${fmtUsd(Math.abs(pnlUsd))}${pnlPctUsd != null ? ` (${pnlPctUsd.toFixed(1)}%)` : ""}`
            : "—"}
        </td>
        {showExpiry && <td>{pos.expiry !== "N/A" ? pos.expiry : "—"}</td>}
      </tr>
      {hasMultipleLegs &&
        legsExpanded &&
        pos.legs.map((leg, i) => {
          const key = legPriceKey(pos.ticker, pos.expiry, leg);
          return (
            <LegRow
              key={`${pos.id}-leg-${i}`}
              leg={leg}
              showExpiry={showExpiry}
              showUnderlying={showUnderlying}
              realtimeLegPrice={key && prices ? prices[key] : null}
              onLegClick={
                readonly || !onLegClick ? undefined : (l) => onLegClick(l, pos)
              }
            />
          );
        })}
    </>
  );
}

/* ─── Position table ───────────────────────────────────── */

export default function PositionTable({
  positions,
  showExpiry = true,
  showUnderlying = false,
  prices,
  fxRates,
  readonly = false,
  hideHeader = false,
  onOrderPlaced,
}: {
  positions: PortfolioPosition[];
  showExpiry?: boolean;
  showUnderlying?: boolean;
  prices?: Record<string, PriceData>;
  /**
   * usd_per_unit FX rates (USD value of 1 unit) from the portfolio payload.
   * Merged with live USD.<cur> ticks via useFx for native→USD display.
   */
  fxRates?: Record<string, number>;
  /**
   * When true, blocks all navigation and order-entry affordances.
   * Load-bearing safety control (tribunal T7): Futu positions render with
   * readonly=true so a click cannot reach /api/orders/place against IB.
   */
  readonly?: boolean;
  /**
   * Suppress the `<thead>` on all-but-the-first instance. Used by
   * PortfolioByStructure so each ticker card shows one header row even
   * though it renders several sub-tables (stock + per-category-pair).
   */
  hideHeader?: boolean;
  onOrderPlaced?: (orderId: string) => void;
}) {
  // Distinct currencies in this table → live usd_per_unit (forex ticks over the
  // payload fallback). USD-only portfolios resolve to { USD: 1 } and every
  // conversion is an identity (no behavior change for the common case).
  const currencies = useMemo(
    () => [
      ...new Set(positions.map((p) => (p.currency || "USD").toUpperCase())),
    ],
    [positions],
  );
  const usdPerUnit = useFx(prices ?? {}, fxRates ?? { USD: 1 }, currencies);
  // Per-currency liveness: a filled FX dot only when THAT pair has a fresh tick.
  const liveCurrencies = currencies.filter(
    (c) => c !== "USD" && (prices?.[`USD.${c}`]?.last ?? null) != null,
  );
  const positionExtract = useMemo(
    () => makePositionExtract(prices, usdPerUnit),
    [prices, usdPerUnit],
  );
  const { sorted, sort, toggle } = useSort(positions, positionExtract);

  // Instrument detail modal state
  const [activeInstrument, setActiveInstrument] = useState<{
    leg: PortfolioLeg;
    ticker: string;
    expiry: string;
  } | null>(null);

  // Order modal state
  const [activeOrderPosition, setActiveOrderPosition] =
    useState<PortfolioPosition | null>(null);

  const handleLegClick = useCallback(
    (leg: PortfolioLeg, pos: PortfolioPosition) => {
      // Readonly tables (Futu tab) must never open the instrument detail
      // modal, which contains a full IB order ticket. Belt + suspenders: the
      // modal render is also gated on !readonly below.
      if (readonly) return;
      setActiveInstrument({ leg, ticker: pos.ticker, expiry: pos.expiry });
    },
    [readonly],
  );

  return (
    <>
      {!hideHeader && (
        <FxBadge rates={usdPerUnit} liveCurrencies={liveCurrencies} />
      )}
      <table style={{ tableLayout: "fixed", width: "100%" }}>
        <colgroup>
          <col style={{ width: "7%" }} />
          <col style={{ width: "14%" }} />
          <col style={{ width: "4%" }} />
          <col style={{ width: "7%" }} />
          {showUnderlying && <col style={{ width: "7%" }} />}
          <col style={{ width: "7%" }} />
          <col style={{ width: "7%" }} />
          <col style={{ width: "6%" }} />
          <col style={{ width: "7%" }} />
          <col style={{ width: "8%" }} />
          <col style={{ width: "8%" }} />
          <col style={{ width: "11%" }} />
          {showExpiry && <col style={{ width: "7%" }} />}
        </colgroup>
        {!hideHeader && (
          <thead>
            <tr>
              <SortTh<PositionSortKey>
                label="Ticker"
                sortKey="ticker"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              <SortTh<PositionSortKey>
                label="Structure"
                sortKey="structure"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              <SortTh<PositionSortKey>
                label="Qty"
                sortKey="qty"
                className="right"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              <SortTh<PositionSortKey>
                label="Direction"
                sortKey="direction"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              {showUnderlying && (
                <SortTh<PositionSortKey>
                  label="Underlying"
                  sortKey="underlying"
                  className="right"
                  activeKey={sort.key}
                  direction={sort.direction}
                  onToggle={toggle}
                />
              )}
              <SortTh<PositionSortKey>
                label="Avg Entry"
                sortKey="avg_entry"
                className="right"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              <SortTh<PositionSortKey>
                label="Last Price"
                sortKey="last_price"
                className="right"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              <SortTh<PositionSortKey>
                label="Day Chg"
                sortKey="daily_chg"
                className="right"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              <SortTh<PositionSortKey>
                label="Today P&L"
                sortKey="today_pnl"
                className="right"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              <SortTh<PositionSortKey>
                label="Entry Cost"
                sortKey="entry_cost"
                className="right"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              <SortTh<PositionSortKey>
                label="Market Value"
                sortKey="market_value"
                className="right"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              <SortTh<PositionSortKey>
                label="P&L"
                sortKey="pnl"
                className="right"
                activeKey={sort.key}
                direction={sort.direction}
                onToggle={toggle}
              />
              {showExpiry && (
                <SortTh<PositionSortKey>
                  label="Expiry"
                  sortKey="expiry"
                  activeKey={sort.key}
                  direction={sort.direction}
                  onToggle={toggle}
                />
              )}
            </tr>
          </thead>
        )}
        <tbody>
          {sorted.map((pos) => (
            <PositionRow
              key={pos.id}
              pos={pos}
              showExpiry={showExpiry}
              showUnderlying={showUnderlying}
              realtimePrice={prices?.[pos.ticker]}
              prices={prices}
              usdPerUnit={usdPerUnit}
              onLegClick={handleLegClick}
              onOrderClick={
                readonly ? undefined : (p) => setActiveOrderPosition(p)
              }
              readonly={readonly}
            />
          ))}
        </tbody>
      </table>

      {!readonly && activeInstrument && prices && (
        <InstrumentDetailModal
          leg={activeInstrument.leg}
          ticker={activeInstrument.ticker}
          expiry={activeInstrument.expiry}
          prices={prices}
          onClose={() => setActiveInstrument(null)}
        />
      )}
      {!readonly && activeOrderPosition && prices && (
        <PositionOrderModal
          position={activeOrderPosition}
          prices={prices}
          onClose={() => setActiveOrderPosition(null)}
          onSubmitted={(orderId) => {
            setActiveOrderPosition(null);
            onOrderPlaced?.(orderId);
          }}
        />
      )}
    </>
  );
}
