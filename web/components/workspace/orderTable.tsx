"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import type {
  ExecutedOrder,
  OpenOrder,
  PortfolioData,
} from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { optionKey } from "@/lib/pricesProtocol";
import { fmtPrice, legPriceKey } from "@/lib/positionUtils";
import {
  type OpenOrderDisplayRow,
  resolveOpenOrderComboPrice,
} from "@/lib/openOrderCombos";

/* ─── Orders tables ────────────────────────────────────── */

export type OpenOrderKey =
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
export function resolveOrderLastPrice(
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

export function makeOpenOrderExtract(
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
/** Wrapper so usePriceDirection can be called per-order row (hooks can't go in map callbacks). */
export function OrderPriceCell({ price }: { price: number | null }) {
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

export type ExecOrderKey =
  | "symbol"
  | "side"
  | "quantity"
  | "avgPrice"
  | "commission"
  | "realizedPNL"
  | "time";

export const execOrderExtract = (
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
