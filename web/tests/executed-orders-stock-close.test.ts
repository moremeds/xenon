import { describe, expect, it } from "vitest";
import { groupExecutedOrders } from "../components/WorkspaceSections";
import type { ExecutedOrder } from "../lib/types";

function stockFill(overrides: Partial<ExecutedOrder>): ExecutedOrder {
  return {
    execId: "x-1",
    symbol: "QQQ",
    contract: {
      conId: 320227571,
      symbol: "QQQ",
      secType: "STK",
      strike: null,
      right: null,
      expiry: null,
    },
    side: "SLD",
    quantity: 1,
    avgPrice: 703.34,
    commission: 0.35,
    realizedPNL: -42.5,
    time: "2026-06-11T19:17:15+00:00",
    exchange: "IBKRATS",
    ...overrides,
  };
}

describe("groupExecutedOrders — stock closing fills", () => {
  it("classifies a stock sell with realized P&L as CLOSE with the P&L total", () => {
    const groups = groupExecutedOrders([stockFill({})]);
    expect(groups).toHaveLength(1);
    expect(groups[0].isClosing).toBe(true);
    expect(groups[0].totalPnL).toBeCloseTo(-42.5);
  });

  it("keeps a stock buy with zero realized P&L as OPEN", () => {
    const groups = groupExecutedOrders([
      stockFill({ execId: "x-2", side: "BOT", realizedPNL: 0 }),
    ]);
    expect(groups[0].isClosing).toBe(false);
    expect(groups[0].totalPnL).toBeNull();
  });
});
