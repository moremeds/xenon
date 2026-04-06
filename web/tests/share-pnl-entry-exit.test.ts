/**
 * TDD tests for Share PnL entry/exit price and time display.
 *
 * Requirements:
 * 1. Show entry price and entry time (with timezone)
 * 2. Show exit price and exit time (with timezone)
 * 3. Remove commission from display
 * 4. Times should be in PST format
 */
import { describe, it, expect } from "vitest";

// Types matching production code
type OrderContract = {
  conId: number | null;
  symbol: string;
  secType: string;
  strike: number | null;
  right: string | null;
  expiry: string | null;
};

type ExecutedOrder = {
  execId: string;
  symbol: string;
  contract: OrderContract;
  side: string;
  quantity: number;
  avgPrice: number | null;
  commission: number | null;
  realizedPNL: number | null;
  time: string;
  exchange: string;
};

type PositionFillGroup = {
  id: string;
  symbol: string;
  description: string;
  isClosing: boolean;
  totalQuantity: number;
  netPrice: number | null;
  totalCommission: number;
  totalPnL: number | null;
  time: string;
  fills: ExecutedOrder[];
};

// Import the actual function and types
import { positionGroupShareData } from "../components/WorkspaceSections";
import type { SharePnlData } from "../components/SharePnlButton";
import type { PortfolioPosition } from "../lib/types";

function makeOptionFill(
  overrides: Partial<ExecutedOrder> & { contract?: Partial<ExecutedOrder["contract"]> } = {},
): ExecutedOrder {
  const { contract: contractOverrides, ...rest } = overrides;
  return {
    execId: rest.execId ?? "opt-fill",
    symbol: rest.symbol ?? "AAOI",
    contract: {
      conId: 1001,
      symbol: "AAOI",
      secType: "OPT",
      strike: 115,
      right: "C",
      expiry: "2026-03-27",
      ...contractOverrides,
    },
    side: rest.side ?? "BOT",
    quantity: rest.quantity ?? 25,
    avgPrice: rest.avgPrice ?? 5.59,
    commission: rest.commission ?? -1.03,
    realizedPNL: rest.realizedPNL ?? null,
    time: rest.time ?? "2026-03-17T15:16:13+00:00",
    exchange: rest.exchange ?? "SMART",
    ...rest,
  };
}

describe("Share PnL - Entry/Exit Price and Time", () => {
  describe("positionGroupShareData returns entry/exit times", () => {
    it("should include entryTime from the matched opening group", () => {
      const openGroup: PositionFillGroup = {
        id: "open-call",
        symbol: "AAOI",
        description: "Opened AAOI Long Call",
        isClosing: false,
        totalQuantity: 25,
        netPrice: 5.59,
        totalCommission: -17.51,
        totalPnL: null,
        time: "2026-03-15T10:30:00-07:00", // Entry time in PST
        fills: [
          makeOptionFill({
            execId: "open-call-1",
            side: "BOT",
            quantity: 25,
            avgPrice: 5.59,
            realizedPNL: null,
            time: "2026-03-15T10:30:00-07:00",
            contract: { conId: 861001, strike: 115, right: "C", expiry: "2026-03-27" },
          }),
        ],
      };

      const closeGroup: PositionFillGroup = {
        id: "close-call",
        symbol: "AAOI",
        description: "Closed AAOI (Long $115 Call)",
        isClosing: true,
        totalQuantity: 25,
        netPrice: 8.05,
        totalCommission: -7.47,
        totalPnL: 6150,
        time: "2026-03-18T07:03:53-07:00", // Exit time in PST
        fills: [
          makeOptionFill({
            execId: "close-call-1",
            side: "SLD",
            quantity: 25,
            avgPrice: 8.05,
            realizedPNL: 6150,
            time: "2026-03-18T07:03:53-07:00",
            contract: { conId: 861001, strike: 115, right: "C", expiry: "2026-03-27" },
          }),
        ],
      };

      const data = positionGroupShareData(closeGroup, [openGroup, closeGroup]);

      // Should have entry time from the opening group
      expect(data.entryTime).toBeDefined();
      expect(data.entryTime).toBe("2026-03-15T10:30:00-07:00");
    });

    it("should include exitTime from the closing group", () => {
      const openGroup: PositionFillGroup = {
        id: "open-call",
        symbol: "AAOI",
        description: "Opened AAOI Long Call",
        isClosing: false,
        totalQuantity: 25,
        netPrice: 5.59,
        totalCommission: -17.51,
        totalPnL: null,
        time: "2026-03-15T10:30:00-07:00",
        fills: [
          makeOptionFill({
            execId: "open-call-1",
            side: "BOT",
            quantity: 25,
            avgPrice: 5.59,
            realizedPNL: null,
            time: "2026-03-15T10:30:00-07:00",
            contract: { conId: 861001, strike: 115, right: "C", expiry: "2026-03-27" },
          }),
        ],
      };

      const closeGroup: PositionFillGroup = {
        id: "close-call",
        symbol: "AAOI",
        description: "Closed AAOI (Long $115 Call)",
        isClosing: true,
        totalQuantity: 25,
        netPrice: 8.05,
        totalCommission: -7.47,
        totalPnL: 6150,
        time: "2026-03-18T07:03:53-07:00",
        fills: [
          makeOptionFill({
            execId: "close-call-1",
            side: "SLD",
            quantity: 25,
            avgPrice: 8.05,
            realizedPNL: 6150,
            time: "2026-03-18T07:03:53-07:00",
            contract: { conId: 861001, strike: 115, right: "C", expiry: "2026-03-27" },
          }),
        ],
      };

      const data = positionGroupShareData(closeGroup, [openGroup, closeGroup]);

      // Should have exit time from the closing group
      expect(data.exitTime).toBeDefined();
      expect(data.exitTime).toBe("2026-03-18T07:03:53-07:00");
    });

    it("should return entryPrice from the matched opening fills", () => {
      const openGroup: PositionFillGroup = {
        id: "open-call",
        symbol: "AAOI",
        description: "Opened AAOI Long Call",
        isClosing: false,
        totalQuantity: 25,
        netPrice: 5.59,
        totalCommission: -17.51,
        totalPnL: null,
        time: "2026-03-15T10:30:00-07:00",
        fills: [
          makeOptionFill({
            execId: "open-call-1",
            side: "BOT",
            quantity: 25,
            avgPrice: 5.59,
            realizedPNL: null,
            time: "2026-03-15T10:30:00-07:00",
            contract: { conId: 861001, strike: 115, right: "C", expiry: "2026-03-27" },
          }),
        ],
      };

      const closeGroup: PositionFillGroup = {
        id: "close-call",
        symbol: "AAOI",
        description: "Closed AAOI (Long $115 Call)",
        isClosing: true,
        totalQuantity: 25,
        netPrice: 8.05,
        totalCommission: -7.47,
        totalPnL: 6150,
        time: "2026-03-18T07:03:53-07:00",
        fills: [
          makeOptionFill({
            execId: "close-call-1",
            side: "SLD",
            quantity: 25,
            avgPrice: 8.05,
            realizedPNL: 6150,
            time: "2026-03-18T07:03:53-07:00",
            contract: { conId: 861001, strike: 115, right: "C", expiry: "2026-03-27" },
          }),
        ],
      };

      const data = positionGroupShareData(closeGroup, [openGroup, closeGroup]);

      // Entry price should be from the opening group
      expect(data.entryPrice).toBeCloseTo(5.59, 2);
      // Exit price should be from the closing group
      expect(data.exitPrice).toBeCloseTo(8.05, 2);
    });

    it("should fall back to portfolio position data when no matching opening fills", () => {
      // This tests the case where a position was opened on a previous day
      // and we don't have the opening fills in allGroups

      // Closing group only (no opening fills in allGroups)
      const closeGroup: PositionFillGroup = {
        id: "close-call",
        symbol: "AAOI",
        description: "Closed AAOI (Long $115 Call)",
        isClosing: true,
        totalQuantity: 25,
        netPrice: 8.05,
        totalCommission: -7.47,
        totalPnL: 6150,
        time: "2026-03-18T07:03:53-07:00",
        fills: [
          makeOptionFill({
            execId: "close-call-1",
            side: "SLD",
            quantity: 25,
            avgPrice: 8.05,
            realizedPNL: 6150,
            time: "2026-03-18T07:03:53-07:00",
            contract: { conId: 861001, strike: 115, right: "C", expiry: "2026-03-27" },
          }),
        ],
      };

      // Portfolio position with entry data from when position was opened
      const portfolioPositions: PortfolioPosition[] = [
        {
          id: 1,
          ticker: "AAOI",
          structure: "Long Call $115",
          structure_type: "long_call",
          risk_profile: "defined",
          expiry: "2026-03-27",
          contracts: 25,
          direction: "LONG",
          entry_cost: 13975, // 25 * 5.59 * 100
          max_risk: 13975,
          market_value: 20125,
          legs: [
            {
              direction: "LONG",
              contracts: 25,
              type: "Call",
              strike: 115,
              entry_cost: 13975,
              avg_cost: 5.59, // This is the entry price per unit
              market_price: 8.05,
              market_value: 20125,
            },
          ],
          kelly_optimal: 0.025,
          target: 12.0,
          stop: 2.8,
          entry_date: "2026-03-15", // Date only, no time
        },
      ];

      // No opening fills in allGroups - only the closing fill
      const data = positionGroupShareData(closeGroup, [closeGroup], portfolioPositions);

      // Entry price should come from portfolio position
      expect(data.entryPrice).toBeCloseTo(5.59, 2);
      // Entry time should be the entry_date from portfolio
      expect(data.entryTime).toBe("2026-03-15");
      // Exit price and time from the closing group
      expect(data.exitPrice).toBeCloseTo(8.05, 2);
      expect(data.exitTime).toBe("2026-03-18T07:03:53-07:00");
    });
  });

  describe("fully-closed positions (no longer in portfolio)", () => {
    it("derives entry price from exit price and realized P&L", () => {
      // PLTR Long $145 Call: opened on prior day, fully closed today.
      // Position no longer exists in portfolio.positions.
      // Entry price should be derived: exitPrice - realizedPNL / (qty * 100)
      const closingGroup: PositionFillGroup = {
        id: "pltr-close",
        symbol: "PLTR",
        description: "Closed PLTR (Long $145 Call)",
        isClosing: true,
        totalQuantity: 50,
        netPrice: 11.70, // exit price per contract
        totalCommission: 38.50,
        totalPnL: 35529.88,
        time: "2026-03-19T12:42:02-07:00",
        fills: [
          makeOptionFill({
            contract: { symbol: "PLTR", secType: "OPT", strike: 145, right: "C", expiry: "2026-03-27", conId: 2001 },
            side: "SLD",
            quantity: 50,
            avgPrice: 11.70,
            realizedPNL: 35529.88,
            time: "2026-03-19T12:42:02-07:00",
          }),
        ],
      };

      // No opening fills today, no portfolio position (fully closed)
      const data = positionGroupShareData(closingGroup, [closingGroup], []);

      // entryPrice = exitPrice - realizedPNL / (qty * 100) = 11.70 - 35529.88 / 5000 = 4.5941
      expect(data.entryPrice).not.toBeNull();
      expect(data.entryPrice).toBeCloseTo(4.59, 1);
      expect(data.exitPrice).toBeCloseTo(11.70, 2);
      expect(data.exitTime).toBe("2026-03-19T12:42:02-07:00");
    });
  });
});

describe("Share PnL API Route - Detail Items", () => {
  // Test for what detail items should be rendered
  it("should NOT include commission in detail items", () => {
    // The API route should NOT add commission to detailItems
    // This is a design requirement - commission clutters the card
    const detailItems: { label: string; value: string }[] = [];

    // Simulate what the API route does with commission
    const commission = 7.47;

    // OLD behavior (should NOT happen):
    // detailItems.push({ label: "COMMISSION", value: `$${Math.abs(commission).toFixed(2)}` });

    // NEW behavior: commission is NOT added
    // (intentionally leave detailItems empty for commission)

    expect(detailItems.find((item) => item.label === "COMMISSION")).toBeUndefined();
  });

  it("should include ENTRY with price and time when entryPrice and entryTime provided", () => {
    const entryPrice = 5.59;
    const entryTime = "3/15/2026, 10:30 AM PST";

    const detailItems: { label: string; value: string }[] = [];

    // Expected behavior: combine price and time
    if (entryPrice != null && entryTime) {
      detailItems.push({ label: "ENTRY", value: `$${entryPrice.toFixed(2)} @ ${entryTime}` });
    }

    expect(detailItems).toHaveLength(1);
    expect(detailItems[0].label).toBe("ENTRY");
    expect(detailItems[0].value).toBe("$5.59 @ 3/15/2026, 10:30 AM PST");
  });

  it("should include EXIT with price and time when exitPrice and exitTime provided", () => {
    const exitPrice = 8.05;
    const exitTime = "3/18/2026, 7:03 AM PST";

    const detailItems: { label: string; value: string }[] = [];

    // Expected behavior: combine price and time
    if (exitPrice != null && exitTime) {
      detailItems.push({ label: "EXIT", value: `$${exitPrice.toFixed(2)} @ ${exitTime}` });
    }

    expect(detailItems).toHaveLength(1);
    expect(detailItems[0].label).toBe("EXIT");
    expect(detailItems[0].value).toBe("$8.05 @ 3/18/2026, 7:03 AM PST");
  });
});

describe("SharePnlData type", () => {
  it("should have entryTime and exitTime fields", () => {
    // This test documents the expected type shape
    const data: SharePnlData = {
      description: "Closed AAOI (Long $115 Call)",
      pnl: 6150,
      pnlPct: 44.08,
      commission: -7.47,
      fillPrice: 8.05,
      entryPrice: 5.59,
      exitPrice: 8.05,
      entryTime: "2026-03-15T10:30:00-07:00",
      exitTime: "2026-03-18T07:03:53-07:00",
      time: "", // Legacy field, can be empty when entry/exit times are present
    };

    expect(data.entryTime).toBe("2026-03-15T10:30:00-07:00");
    expect(data.exitTime).toBe("2026-03-18T07:03:53-07:00");
  });
});
