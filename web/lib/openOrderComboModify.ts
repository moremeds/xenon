import type { OpenOrderComboRow } from "./openOrderCombos";
import type { ModifyCancelTarget } from "./orderModify";
import type { OpenOrder, OrderComboLeg } from "./types";

function gcd(a: number, b: number): number {
  let x = Math.abs(Math.round(a));
  let y = Math.abs(Math.round(b));
  while (y !== 0) {
    const next = x % y;
    x = y;
    y = next;
  }
  return x || 1;
}

function normalizeComboQuantity(orders: OpenOrder[]): number {
  const quantities = orders
    .map((order) => Math.abs(Math.round(order.totalQuantity)))
    .filter((quantity) => quantity > 0);

  if (quantities.length === 0) return 1;
  return quantities.reduce((acc, quantity) => gcd(acc, quantity), quantities[0]);
}

function buildComboLegs(orders: OpenOrder[], baseQuantity: number): OrderComboLeg[] {
  return orders.map((order) => ({
    conId: order.contract.conId ?? 0,
    ratio: Math.max(1, Math.round(Math.abs(order.totalQuantity) / baseQuantity)),
    action: order.action,
    symbol: order.contract.symbol,
    strike: order.contract.strike,
    right: order.contract.right,
    expiry: order.contract.expiry,
  }));
}

export function buildGroupedComboModifyTarget(row: OpenOrderComboRow): {
  modalOrder: OpenOrder;
  cancelOrders: ModifyCancelTarget[];
} {
  const firstOrder = row.orders[0];
  const baseQuantity = normalizeComboQuantity(row.orders);

  return {
    modalOrder: {
      orderId: firstOrder.orderId,
      permId: firstOrder.permId,
      symbol: row.symbol,
      contract: {
        conId: firstOrder.contract.conId,
        symbol: row.symbol,
        secType: "BAG",
        strike: null,
        right: null,
        expiry: null,
        comboLegs: buildComboLegs(row.orders, baseQuantity),
      },
      // Synthetic grouped rows do not have a canonical IB BAG action. Use BUY so
      // the combo replacement preserves the leg actions as entered.
      action: "BUY",
      orderType: firstOrder.orderType,
      totalQuantity: baseQuantity,
      limitPrice: row.limitPrice,
      auxPrice: firstOrder.auxPrice,
      status: row.status,
      filled: 0,
      remaining: baseQuantity,
      avgFillPrice: null,
      tif: row.tif === "MIXED" ? firstOrder.tif : row.tif,
    },
    cancelOrders: row.orders.map((order) => ({
      orderId: order.orderId,
      permId: order.permId,
    })),
  };
}
