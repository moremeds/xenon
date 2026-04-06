"use client";

/**
 * OrderPriceStrip — Displays BID/MID/ASK/SPREAD in a horizontal strip
 *
 * Usage:
 *   <OrderPriceStrip prices={orderPrices} />
 *   <OrderPriceStrip prices={orderPrices} compact />
 */

import type { OrderPrices } from "../types";

interface OrderPriceStripProps {
  prices: OrderPrices;
  /** Compact mode for tighter spaces */
  compact?: boolean;
  /** Hide spread column */
  hideSpread?: boolean;
  /** Custom class name */
  className?: string;
}

function formatPrice(value: number | null): string {
  if (value == null) return "---";
  return `$${value.toFixed(2)}`;
}

function formatPct(value: number | null): string {
  if (value == null) return "";
  return `(${value.toFixed(1)}%)`;
}

export function OrderPriceStrip({
  prices,
  compact = false,
  hideSpread = false,
  className = "",
}: OrderPriceStripProps) {
  const baseClass = compact ? "order-price-strip-compact" : "order-price-strip";

  return (
    <div className={`${baseClass} ${className}`.trim()}>
      <div className="order-price-item">
        <span className="order-price-label">BID</span>
        <span className="order-price-value order-price-bid">
          {formatPrice(prices.bid)}
        </span>
      </div>
      <div className="order-price-item">
        <span className="order-price-label">MID</span>
        <span className="order-price-value">
          {formatPrice(prices.mid)}
        </span>
      </div>
      <div className="order-price-item">
        <span className="order-price-label">ASK</span>
        <span className="order-price-value order-price-ask">
          {formatPrice(prices.ask)}
        </span>
      </div>
      {!hideSpread && (
        <div className="order-price-item order-price-spread">
          <span className="order-price-label">SPREAD</span>
          <span className="order-price-value">
            {formatPrice(prices.spread)}
            {prices.spreadPct != null && (
              <span className="order-price-pct"> {formatPct(prices.spreadPct)}</span>
            )}
          </span>
        </div>
      )}
    </div>
  );
}
