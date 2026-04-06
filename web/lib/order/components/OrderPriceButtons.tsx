"use client";

/**
 * OrderPriceButtons — Quick-fill BID/MID/ASK buttons
 *
 * Usage:
 *   <OrderPriceButtons prices={orderPrices} onSelect={setLimitPrice} />
 *   <OrderPriceButtons prices={orderPrices} onSelect={setLimitPrice} showPrices />
 */

import type { OrderPrices } from "../types";

interface OrderPriceButtonsProps {
  prices: OrderPrices;
  /** Callback when a price is selected */
  onSelect: (price: string) => void;
  /** Show price values in the buttons */
  showPrices?: boolean;
  /** Disabled state */
  disabled?: boolean;
  /** Custom class name */
  className?: string;
}

function formatButtonPrice(value: number | null): string {
  if (value == null) return "";
  return ` $${value.toFixed(2)}`;
}

export function OrderPriceButtons({
  prices,
  onSelect,
  showPrices = true,
  disabled = false,
  className = "",
}: OrderPriceButtonsProps) {
  const handleBid = () => {
    if (prices.bid != null) onSelect(prices.bid.toFixed(2));
  };

  const handleMid = () => {
    if (prices.mid != null) onSelect(prices.mid.toFixed(2));
  };

  const handleAsk = () => {
    if (prices.ask != null) onSelect(prices.ask.toFixed(2));
  };

  return (
    <div className={`order-price-buttons ${className}`.trim()}>
      <button
        type="button"
        className="btn-quick order-price-btn-bid"
        disabled={disabled || prices.bid == null}
        onClick={handleBid}
      >
        BID{showPrices && formatButtonPrice(prices.bid)}
      </button>
      <button
        type="button"
        className="btn-quick"
        disabled={disabled || prices.mid == null}
        onClick={handleMid}
      >
        MID{showPrices && formatButtonPrice(prices.mid)}
      </button>
      <button
        type="button"
        className="btn-quick order-price-btn-ask"
        disabled={disabled || prices.ask == null}
        onClick={handleAsk}
      >
        ASK{showPrices && formatButtonPrice(prices.ask)}
      </button>
    </div>
  );
}
