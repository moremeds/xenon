/**
 * Unified Order System
 *
 * Composable components, hooks, and types for consistent order
 * placement, modification, and display across the application.
 *
 * Usage:
 *   import { OrderPriceStrip, OrderAction } from "@/lib/order";
 */

// Types
export type {
  OrderAction,
  OrderTif,
  OrderPrices,
  OrderLeg,
  OrderSummary,
} from "./types";

export {
  buildFastApiPlaceOrderPayload,
  buildPlaceOrderBody,
} from "./placeOrderContract";

// Components
export { OrderPriceStrip } from "./components/OrderPriceStrip";
export { OrderLegPills } from "./components/OrderLegPills";
export { OrderTifSelector } from "./components/OrderTifSelector";
export { OrderConfirmSummary } from "./components/OrderConfirmSummary";
