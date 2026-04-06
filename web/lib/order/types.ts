/**
 * Unified Order System Types
 *
 * Shared type definitions for the order component system.
 */

export type OrderAction = "BUY" | "SELL";
export type OrderTif = "DAY" | "GTC";
export type OrderType = "stock" | "option" | "combo";

/** Computed prices for an order (single or spread) */
export interface OrderPrices {
  bid: number | null;
  mid: number | null;
  ask: number | null;
  spread: number | null;      // ask - bid
  spreadPct: number | null;   // spread / mid * 100
  available: boolean;         // true if all prices resolved
}

/** A single leg in a multi-leg order */
export interface OrderLeg {
  id: string;
  action: OrderAction;
  direction: "LONG" | "SHORT";
  strike: number;
  type: "Call" | "Put";
  expiry: string;
  quantity: number;
  bid?: number | null;
  ask?: number | null;
}

/** Order form state */
export interface OrderFormState {
  action: OrderAction;
  quantity: string;
  limitPrice: string;
  tif: OrderTif;
  confirmStep: boolean;
  loading: boolean;
  error: string | null;
  success: string | null;
}

/** Validation result */
export interface OrderValidation {
  isValid: boolean;
  errors: {
    quantity?: string;
    price?: string;
    general?: string;
  };
  parsedQuantity: number;
  parsedPrice: number;
}

/** Order summary for confirmation */
export interface OrderSummary {
  description: string;        // "BUY 44x GOOG Bull Call Spread @ $6.50"
  totalCost: number | null;   // quantity * price * 100 for options
  totalLabel?: string;        // override for close/debit/credit semantics
  maxGain?: number | null;    // For spreads
  maxLoss?: number | null;    // For spreads
  breakeven?: number | null;  // For options/spreads
  estimatedPnl?: number | null;
  estimatedPnlLabel?: string;
}

/** Props for price-related components */
export interface PriceDisplayProps {
  prices: OrderPrices;
  showSpread?: boolean;
  compact?: boolean;
}

/** Props for leg display components */
export interface LegDisplayProps {
  legs: OrderLeg[];
  compact?: boolean;
  showPrices?: boolean;
}

/** Common order form props */
export interface OrderFormProps {
  ticker: string;
  type: OrderType;
  legs?: OrderLeg[];
  defaultAction?: OrderAction;
  defaultQuantity?: number;
  onOrderPlaced?: () => void;
}
