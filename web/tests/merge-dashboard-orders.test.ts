import { describe, it, expect } from "vitest";
import { mergeDashboardOrders } from "@/lib/mergeDashboardOrders";
import type { OrdersData, OpenOrder } from "@/lib/types";

function mkOrder(symbol: string): OpenOrder {
  return {
    orderId: 0,
    permId: 0,
    symbol,
    contract: { symbol, secType: "STK" } as OpenOrder["contract"],
    action: "BUY",
    orderType: "LMT",
    totalQuantity: 1,
    limitPrice: 1,
    auxPrice: null,
    status: "Submitted",
    filled: 0,
    remaining: 1,
    avgFillPrice: null,
    tif: "GTC",
  };
}

function mkData(syms: string[], lastSync: string): OrdersData {
  return {
    last_sync: lastSync,
    open_orders: syms.map(mkOrder),
    executed_orders: [],
    open_count: syms.length,
    executed_count: 0,
  };
}

describe("mergeDashboardOrders", () => {
  it("merges both brokers' open orders and tags each with its broker", () => {
    const ib = mkData(["QQQ"], "2026-06-17T10:00:00Z");
    const futu = mkData(["TSLA"], "2026-06-17T11:00:00Z");
    const merged = mergeDashboardOrders(ib, futu);

    expect(merged.open_orders.map((o) => o.symbol)).toEqual(["QQQ", "TSLA"]);
    expect(merged.open_orders.find((o) => o.symbol === "QQQ")?.broker).toBe(
      "IB",
    );
    expect(merged.open_orders.find((o) => o.symbol === "TSLA")?.broker).toBe(
      "FUTU",
    );
    expect(merged.open_count).toBe(2);
    // Latest of the two sync stamps wins.
    expect(merged.last_sync).toBe("2026-06-17T11:00:00Z");
  });

  it("does not mutate the source orders (tags a copy)", () => {
    const ib = mkData(["QQQ"], "2026-06-17T10:00:00Z");
    mergeDashboardOrders(ib, null);
    expect(ib.open_orders[0].broker).toBeUndefined();
  });

  it("tolerates a null broker snapshot (one tab never synced)", () => {
    const futu = mkData(["TSLA", "AAPL"], "2026-06-17T11:00:00Z");
    const merged = mergeDashboardOrders(null, futu);
    expect(merged.open_orders).toHaveLength(2);
    expect(merged.open_orders.every((o) => o.broker === "FUTU")).toBe(true);
    expect(merged.open_count).toBe(2);
  });

  it("returns an empty snapshot when both are null", () => {
    const merged = mergeDashboardOrders(null, null);
    expect(merged.open_orders).toEqual([]);
    expect(merged.open_count).toBe(0);
    expect(merged.last_sync).toBe("");
  });
});
