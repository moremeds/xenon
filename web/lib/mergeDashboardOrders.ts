import type { OrdersData, OpenOrder } from "./types";

/**
 * Merge two brokers' order snapshots into one for the dashboard's combined
 * "IB + FUTU" Working & Filled card.
 *
 * The dashboard shows a merged portfolio across both brokers, so its orders
 * card must do the same — otherwise it shows only the active tab's broker and
 * silently drops the other's working orders (e.g. an IB working order vanishes
 * while the FUTU tab is active). Each open order is tagged with its `broker`
 * so the card can label IB (actionable) vs FUTU (read-only) rows.
 *
 * Counts sum the per-broker totals (which reflect the true count before the
 * card's top-3 slice), falling back to the merged array length when a snapshot
 * omits its count.
 */
export function mergeDashboardOrders(
  ib: OrdersData | null,
  futu: OrdersData | null,
): OrdersData {
  const tag = (o: OpenOrder, broker: "IB" | "FUTU"): OpenOrder => ({
    ...o,
    broker,
  });
  const ibOpen = (ib?.open_orders ?? []).map((o) => tag(o, "IB"));
  const futuOpen = (futu?.open_orders ?? []).map((o) => tag(o, "FUTU"));
  const ibFills = ib?.executed_orders ?? [];
  const futuFills = futu?.executed_orders ?? [];

  // Latest of the two sync timestamps (ISO strings sort lexicographically).
  const lastSync =
    [ib?.last_sync, futu?.last_sync].filter(Boolean).sort().pop() ?? "";

  return {
    last_sync: lastSync,
    open_orders: [...ibOpen, ...futuOpen],
    executed_orders: [...ibFills, ...futuFills],
    open_count:
      (ib?.open_count ?? ibOpen.length) + (futu?.open_count ?? futuOpen.length),
    executed_count:
      (ib?.executed_count ?? ibFills.length) +
      (futu?.executed_count ?? futuFills.length),
  };
}
