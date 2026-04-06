/**
 * Unified Order System
 *
 * Composable components, hooks, and types for consistent order
 * placement, modification, and display across the application.
 *
 * Usage:
 *   import { OrderPriceStrip, useOrderPrices, OrderAction } from "@/lib/order";
 */

// Types
export type {
  OrderAction,
  OrderTif,
  OrderType,
  OrderPrices,
  OrderLeg,
  OrderFormState,
  OrderValidation,
  OrderSummary,
  PriceDisplayProps,
  LegDisplayProps,
  OrderFormProps,
} from "./types";

// Hooks
export { useOrderPrices } from "./hooks/useOrderPrices";
export { useOrderValidation } from "./hooks/useOrderValidation";

// Components
export { OrderPriceStrip } from "./components/OrderPriceStrip";
export { OrderLegPills } from "./components/OrderLegPills";
export { OrderPriceButtons } from "./components/OrderPriceButtons";
export { OrderActionToggle } from "./components/OrderActionToggle";
export { OrderTifSelector } from "./components/OrderTifSelector";
export { OrderQuantityInput } from "./components/OrderQuantityInput";
export { OrderPriceInput } from "./components/OrderPriceInput";
export { OrderConfirmSummary } from "./components/OrderConfirmSummary";
