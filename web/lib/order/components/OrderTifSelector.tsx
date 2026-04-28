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
  const dayActive = tif === "DAY";
  const gtcActive = tif === "GTC";

  return (
    <div
      className={`order-tif-selector ${className}`.trim()}
      role="group"
      aria-label="Time in force"
    >
      <button
        type="button"
        className={`order-action-btn order-tif-btn ${dayActive ? "order-action-active order-tif-active" : ""}`}
        aria-pressed={dayActive}
        disabled={disabled}
        onClick={() => onChange("DAY")}
      >
        DAY
      </button>
      <button
        type="button"
        className={`order-action-btn order-tif-btn ${gtcActive ? "order-action-active order-tif-active" : ""}`}
        aria-pressed={gtcActive}
        disabled={disabled}
        onClick={() => onChange("GTC")}
      >
        GTC
      </button>
    </div>
  );
}
