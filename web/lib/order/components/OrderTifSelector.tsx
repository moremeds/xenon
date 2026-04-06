"use client";

/**
 * OrderTifSelector — DAY/GTC time-in-force toggle
 *
 * Usage:
 *   <OrderTifSelector tif={tif} onChange={setTif} />
 */

import type { OrderTif } from "../types";

interface OrderTifSelectorProps {
  tif: OrderTif;
  onChange: (tif: OrderTif) => void;
  /** Disabled state */
  disabled?: boolean;
  /** Custom class name */
  className?: string;
}

export function OrderTifSelector({
  tif,
  onChange,
  disabled = false,
  className = "",
}: OrderTifSelectorProps) {
  return (
    <div className={`order-tif-selector ${className}`.trim()}>
      <button
        type="button"
        className={`order-action-btn ${tif === "DAY" ? "order-action-active" : ""}`}
        disabled={disabled}
        onClick={() => onChange("DAY")}
      >
        DAY
      </button>
      <button
        type="button"
        className={`order-action-btn ${tif === "GTC" ? "order-action-active" : ""}`}
        disabled={disabled}
        onClick={() => onChange("GTC")}
      >
        GTC
      </button>
    </div>
  );
}
