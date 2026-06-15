/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { OrdersSnapshotCard } from "@/components/dashboard/OrdersSnapshotCard";
import type { OrdersData, OpenOrder, ExecutedOrder } from "@/lib/types";

afterEach(() => cleanup());

function openOrder(over: Partial<OpenOrder> & { orderId: number }): OpenOrder {
  return {
    orderId: over.orderId,
    permId: over.permId ?? over.orderId,
    symbol: over.symbol ?? "QQQ",
    contract: over.contract ?? {
      conId: 1,
      symbol: "QQQ",
      secType: "OPT",
      strike: 630,
      right: "C",
      expiry: "20260619",
    },
    action: over.action ?? "BUY",
    orderType: over.orderType ?? "LMT",
    totalQuantity: over.totalQuantity ?? 2,
    limitPrice: over.limitPrice ?? 1.25,
    auxPrice: null,
    status: over.status ?? "Submitted",
    filled: 0,
    remaining: over.totalQuantity ?? 2,
    avgFillPrice: null,
    tif: "DAY",
  };
}

function fill(
  over: Partial<ExecutedOrder> & { execId: string },
): ExecutedOrder {
  return {
    execId: over.execId,
    symbol: over.symbol ?? "QQQ",
    contract: over.contract ?? {
      conId: 2,
      symbol: "QQQ",
      secType: "OPT",
      strike: 600,
      right: "P",
      expiry: "20260619",
    },
    side: over.side ?? "SELL",
    quantity: over.quantity ?? 1,
    avgPrice: over.avgPrice ?? 3.4,
    commission: null,
    realizedPNL: null,
    time: over.time ?? "2026-06-15T14:05:00Z",
    exchange: "SMART",
  };
}

function ordersData(over: Partial<OrdersData>): OrdersData {
  return {
    last_sync: "2026-06-15T14:00:00Z",
    open_orders: over.open_orders ?? [],
    executed_orders: over.executed_orders ?? [],
    open_count: over.open_orders?.length ?? 0,
    executed_count: over.executed_orders?.length ?? 0,
  };
}

describe("OrdersSnapshotCard", () => {
  it("renders the empty state when orders is null", () => {
    render(<OrdersSnapshotCard orders={null} />);
    expect(screen.getByText("No open or filled orders today.")).toBeTruthy();
  });

  it("links to /orders", () => {
    render(
      <OrdersSnapshotCard
        orders={ordersData({ open_orders: [openOrder({ orderId: 1 })] })}
      />,
    );
    const link = screen.getByText("All orders →") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/orders");
  });

  it("caps the working list at 3 but shows the true count", () => {
    const orders = ordersData({
      open_orders: [1, 2, 3, 4].map((n) => openOrder({ orderId: n })),
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    const lists = container.querySelectorAll(".snapshot-list");
    const workingRows = lists[0].querySelectorAll(".snapshot-list__row");
    expect(workingRows.length).toBe(3);
    expect(lists[0].querySelector(".snapshot-list__count")?.textContent).toBe(
      "4",
    );
  });

  it("describes OPT, BAG, and stock open orders", () => {
    const orders = ordersData({
      open_orders: [
        openOrder({
          orderId: 1,
          action: "BUY",
          totalQuantity: 2,
          limitPrice: 1.25,
          contract: {
            conId: 1,
            symbol: "QQQ",
            secType: "OPT",
            strike: 630,
            right: "C",
            expiry: "20260619",
          },
        }),
        openOrder({
          orderId: 2,
          action: "BUY",
          totalQuantity: 1,
          limitPrice: 2.5,
          contract: {
            conId: 2,
            symbol: "SPY",
            secType: "BAG",
            strike: null,
            right: null,
            expiry: null,
          },
        }),
        openOrder({
          orderId: 3,
          action: "SELL",
          totalQuantity: 100,
          limitPrice: 150,
          contract: {
            conId: 3,
            symbol: "AAPL",
            secType: "STK",
            strike: null,
            right: null,
            expiry: null,
          },
        }),
      ],
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    const text = container.textContent ?? "";
    expect(text).toContain("BUY 2× QQQ Call $630 @ $1.25");
    expect(text).toContain("BUY 1× SPY combo @ $2.50");
    expect(text).toContain("SELL 100× AAPL @ $150.00");
  });

  it("renders a credit combo limit price with a leading minus, not $-", () => {
    const orders = ordersData({
      open_orders: [
        openOrder({
          orderId: 1,
          action: "SELL",
          totalQuantity: 1,
          limitPrice: -0.4, // credit combo
          contract: {
            conId: 1,
            symbol: "SPY",
            secType: "BAG",
            strike: null,
            right: null,
            expiry: null,
          },
        }),
      ],
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    expect(container.textContent).toContain("SELL 1× SPY combo @ -$0.40");
    expect(container.textContent).not.toContain("$-0.40");
  });

  it("guards nullable OPT strike so it never renders $null", () => {
    const orders = ordersData({
      open_orders: [
        openOrder({
          orderId: 1,
          action: "BUY",
          totalQuantity: 2,
          limitPrice: 1.25,
          contract: {
            conId: 1,
            symbol: "QQQ",
            secType: "OPT",
            strike: null,
            right: "C",
            expiry: "20260619",
          },
        }),
      ],
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    expect(container.textContent).toContain("BUY 2× QQQ Call @ $1.25");
    expect(container.textContent).not.toContain("$null");
  });

  it("normalizes BOT/SLD fill sides to BUY/SELL and shows the fill time", () => {
    const orders = ordersData({
      executed_orders: [
        fill({
          execId: "e1",
          side: "SLD", // real IB value from orders.py (not "SELL")
          quantity: 1,
          avgPrice: 3.4,
          time: "2026-06-15T14:05:00Z",
          contract: {
            conId: 9,
            symbol: "QQQ",
            secType: "OPT",
            strike: 600,
            right: "P",
            expiry: "20260619",
          },
        }),
      ],
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    expect(container.textContent).toContain("SELL 1× QQQ Put $600 @ $3.40");
    expect(container.textContent).not.toContain("SLD");
    // TZ-agnostic: assert an HH:MM time rendered, not an exact local string.
    const fillsList = container.querySelectorAll(".snapshot-list")[1];
    expect(
      fillsList.querySelector(".snapshot-list__row-meta")?.textContent,
    ).toMatch(/\d{2}:\d{2}/);
  });

  it("caps the fills list at 3 but shows the true count", () => {
    const orders = ordersData({
      executed_orders: [1, 2, 3, 4].map((n) =>
        fill({ execId: `e${n}`, side: "BOT" }),
      ),
    });
    const { container } = render(<OrdersSnapshotCard orders={orders} />);
    const fillsList = container.querySelectorAll(".snapshot-list")[1];
    expect(fillsList.querySelectorAll(".snapshot-list__row").length).toBe(3);
    expect(fillsList.querySelector(".snapshot-list__count")?.textContent).toBe(
      "4",
    );
  });
});
