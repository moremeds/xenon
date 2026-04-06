"use client";

/**
 * OrderLegPills — Displays combo legs as colored pills
 *
 * Usage:
 *   <OrderLegPills legs={orderLegs} />
 *   <OrderLegPills legs={orderLegs} showPrices prices={priceData} />
 */

import type { OrderLeg } from "../types";

interface OrderLegPillsProps {
  legs: OrderLeg[];
  /** Show leg prices inline */
  showPrices?: boolean;
  /** Compact mode for tighter spaces */
  compact?: boolean;
  /** Custom class name */
  className?: string;
}

function formatLegPrice(leg: OrderLeg): string {
  if (leg.bid == null || leg.ask == null) return "";
  const mid = (leg.bid + leg.ask) / 2;
  return `@${mid.toFixed(2)}`;
}

export function OrderLegPills({
  legs,
  showPrices = false,
  compact = false,
  className = "",
}: OrderLegPillsProps) {
  const baseClass = compact ? "order-leg-pills-compact" : "order-leg-pills";

  return (
    <div className={`${baseClass} ${className}`.trim()}>
      {legs.map((leg, i) => {
        const isLong = leg.direction === "LONG";
        const pillClass = isLong ? "order-leg-pill-long" : "order-leg-pill-short";
        const dirSymbol = isLong ? "+" : "−";

        return (
          <div key={leg.id || i} className={`order-leg-pill ${pillClass}`}>
            <span className="order-leg-dir">{dirSymbol}</span>
            <span className="order-leg-strike">
              ${leg.strike} {leg.type}
            </span>
            {showPrices && (
              <span className="order-leg-price">{formatLegPrice(leg)}</span>
            )}
          </div>
        );
      })}
    </div>
  );
}
