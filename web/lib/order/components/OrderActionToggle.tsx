"use client";

/**
 * OrderActionToggle — BUY/SELL toggle buttons
 *
 * Usage:
 *   <OrderActionToggle action={action} onChange={setAction} />
 */

import type { OrderAction } from "../types";

interface OrderActionToggleProps {
  action: OrderAction;
  onChange: (action: OrderAction) => void;
  /** Disabled state */
  disabled?: boolean;
  /** Custom class name */
  className?: string;
}

export function OrderActionToggle({
  action,
  onChange,
  disabled = false,
  className = "",
}: OrderActionToggleProps) {
  return (
    <div className={`order-action-toggle ${className}`.trim()}>
      <button
        type="button"
        className={`order-action-btn ${action === "BUY" ? "order-action-active order-action-buy" : ""}`}
        disabled={disabled}
        onClick={() => onChange("BUY")}
      >
        BUY
      </button>
      <button
        type="button"
        className={`order-action-btn ${action === "SELL" ? "order-action-active order-action-sell" : ""}`}
        disabled={disabled}
        onClick={() => onChange("SELL")}
      >
        SELL
      </button>
    </div>
  );
}
