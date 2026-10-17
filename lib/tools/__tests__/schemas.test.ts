import { describe, it, expect } from "vitest";
import { Value } from "@sinclair/typebox/value";
import { OrdersData } from "../schemas/ib-orders";
import { PortfolioData } from "../schemas/ib-sync";
import { IBOrderManageOutput } from "../schemas/ib-order-manage";

describe("OrdersData schema", () => {
  it("accepts empty orders", () => {
    const valid = {
      last_sync: "2026-03-04T10:00:00",
      open_orders: [],
      executed_orders: [],
      open_count: 0,
      executed_count: 0,
    };
    expect(Value.Check(OrdersData, valid)).toBe(true);
  });
});

describe("PortfolioData schema", () => {
  it("accepts valid portfolio data", () => {
    const valid = {
      bankroll: 100000,
      peak_value: 105000,
      last_sync: "2026-03-04T10:00:00",
      positions: [],
      total_deployed_pct: 15.5,
      total_deployed_dollars: 15500,
      remaining_capacity_pct: 84.5,
      position_count: 0,
      defined_risk_count: 0,
      undefined_risk_count: 0,
      avg_kelly_optimal: null,
    };
    expect(Value.Check(PortfolioData, valid)).toBe(true);
  });
});

describe("IBOrderManageOutput schema", () => {
  it("accepts cancel success", () => {
    const valid = {
      status: "ok" as const,
      message: "Order cancelled (orderId=10)",
      orderId: 10,
      finalStatus: "Cancelled",
    };
    expect(Value.Check(IBOrderManageOutput, valid)).toBe(true);
  });

  it("accepts error", () => {
    const valid = {
      status: "error" as const,
      message: "Trade not found",
    };
    expect(Value.Check(IBOrderManageOutput, valid)).toBe(true);
  });
});
