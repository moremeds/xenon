"use client";

/**
 * OrderConfirmSummary — Order summary panel for confirmation step
 *
 * Usage:
 *   <OrderConfirmSummary summary={orderSummary} />
 */

import type { OrderSummary } from "../types";

interface OrderConfirmSummaryProps {
  summary: OrderSummary;
  /** Show as info callout (blue) or neutral */
  variant?: "info" | "neutral";
  /** Custom class name */
  className?: string;
}

function formatCurrency(value: number | null | undefined): string {
  if (value == null) return "---";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

function formatPrice(value: number | null | undefined): string {
  if (value == null) return "---";
  return `$${value.toFixed(2)}`;
}

export function OrderConfirmSummary({
  summary,
  variant = "info",
  className = "",
}: OrderConfirmSummaryProps) {
  const variantClass = variant === "info" ? "order-confirm-summary-info" : "";

  return (
    <div className={`order-confirm-summary ${variantClass} ${className}`.trim()}>
      <div className="order-confirm-description">{summary.description}</div>
      <div className="order-confirm-metrics">
        {summary.totalCost != null && (
          <span className="order-confirm-metric">
            <span className="order-confirm-metric-label">{summary.totalLabel ?? "Total:"}</span>
            <span className="order-confirm-metric-value">{formatCurrency(summary.totalCost)}</span>
          </span>
        )}
        {summary.maxGain != null && (
          <span className="order-confirm-metric">
            <span className="order-confirm-metric-label">Max Gain:</span>
            <span className="order-confirm-metric-value order-confirm-positive">
              {formatCurrency(summary.maxGain)}
            </span>
          </span>
        )}
        {summary.maxLoss != null && (
          <span className="order-confirm-metric">
            <span className="order-confirm-metric-label">Max Loss:</span>
            <span className="order-confirm-metric-value order-confirm-negative">
              {formatCurrency(summary.maxLoss)}
            </span>
          </span>
        )}
        {summary.breakeven != null && (
          <span className="order-confirm-metric">
            <span className="order-confirm-metric-label">Breakeven:</span>
            <span className="order-confirm-metric-value">{formatPrice(summary.breakeven)}</span>
          </span>
        )}
        {summary.estimatedPnl != null && (
          <span className="order-confirm-metric">
            <span className="order-confirm-metric-label">{summary.estimatedPnlLabel ?? "Est. P&L:"}</span>
            <span className={`order-confirm-metric-value ${summary.estimatedPnl >= 0 ? "order-confirm-positive" : "order-confirm-negative"}`}>
              {formatCurrency(summary.estimatedPnl)}
            </span>
          </span>
        )}
      </div>
    </div>
  );
}
