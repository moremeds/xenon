import { describe, it, expect } from "vitest";
import { buildCloseTicket } from "@/lib/positionOrderPresets";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { legPriceKey } from "@/lib/positionUtils";

function stockPos(
  overrides: Partial<PortfolioPosition> = {},
): PortfolioPosition {
  return {
    id: 1,
    ticker: "TSLA",
    structure: "Stock",
    structure_type: "Stock",
    risk_profile: "equity",
    expiry: "",
    contracts: 300,
    direction: "LONG",
    entry_cost: 96000,
    max_risk: null,
    market_value: 105000,
    legs: [
      {
        direction: "LONG",
        contracts: 300,
        type: "Stock",
        strike: null,
        entry_cost: 96000,
        avg_cost: 320,
        market_price: 350,
        market_value: 105000,
        market_price_is_calculated: false,
      },
    ],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "",
    ...overrides,
  };
}

describe("buildCloseTicket — stock", () => {
  const prices: Record<string, PriceData> = {
    TSLA: { last: 350, bid: 349.9, ask: 350.1, close: 345 } as PriceData,
  };

  it("LONG stock → SELL full qty at last price", () => {
    const draft = buildCloseTicket(
      stockPos({ direction: "LONG", contracts: 300 }),
      prices,
    );
    expect(draft.payload.type).toBe("stock");
    expect(draft.payload.action).toBe("SELL");
    expect(draft.payload.quantity).toBe(300);
    expect(draft.payload.symbol).toBe("TSLA");
    expect(draft.payload.limitPrice).toBe(350);
    expect(draft.payload.tif).toBe("DAY");
  });

  it("SHORT stock → BUY full qty", () => {
    const draft = buildCloseTicket(
      stockPos({ direction: "SHORT", contracts: 300 }),
      prices,
    );
    expect(draft.payload.action).toBe("BUY");
    expect(draft.payload.quantity).toBe(300);
  });

  it("uses bid/ask mid when last is missing", () => {
    const draft = buildCloseTicket(stockPos(), {
      TSLA: { last: null, bid: 349, ask: 351, close: 345 } as PriceData,
    });
    expect(draft.payload.limitPrice).toBe(350);
  });
});

function singleLegOptionPos(overrides: {
  direction: "LONG" | "SHORT";
  type: "Call" | "Put";
  strike: number;
  expiry: string; // YYYY-MM-DD
  contracts: number;
}): PortfolioPosition {
  const { direction, type, strike, expiry, contracts } = overrides;
  return {
    id: 2,
    ticker: "AAPL",
    structure: type === "Call" ? "Long Call" : "Long Put",
    structure_type: type === "Call" ? "LongCall" : "LongPut",
    risk_profile: "defined",
    expiry,
    contracts,
    direction,
    entry_cost: 500,
    max_risk: 500,
    market_value: 600,
    legs: [
      {
        direction,
        contracts,
        type,
        strike,
        entry_cost: 500,
        avg_cost: 5,
        market_price: 6,
        market_value: 600,
        market_price_is_calculated: false,
      },
    ],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "",
  };
}

describe("buildCloseTicket — single-leg option", () => {
  const expiry = "2026-06-19";
  const pos = singleLegOptionPos({
    direction: "LONG",
    type: "Call",
    strike: 200,
    expiry,
    contracts: 5,
  });
  const key = legPriceKey("AAPL", expiry, pos.legs[0])!;
  const prices: Record<string, PriceData> = {
    [key]: { last: 6, bid: 5.9, ask: 6.1, close: 5 } as PriceData,
  };

  it("LONG call → SELL-to-close with option payload fields", () => {
    const draft = buildCloseTicket(pos, prices);
    expect(draft.payload.type).toBe("option");
    expect(draft.payload.action).toBe("SELL");
    expect(draft.payload.quantity).toBe(5);
    if (draft.payload.type === "option") {
      expect(draft.payload.strike).toBe(200);
      expect(draft.payload.right).toBe("C");
      expect(draft.payload.expiry).toBe("20260619");
      expect(draft.payload.limitPrice).toBe(6);
    }
  });

  it("SHORT put → BUY-to-close", () => {
    const shortPut = singleLegOptionPos({
      direction: "SHORT",
      type: "Put",
      strike: 180,
      expiry,
      contracts: 2,
    });
    const k = legPriceKey("AAPL", expiry, shortPut.legs[0])!;
    const draft = buildCloseTicket(shortPut, {
      [k]: { last: 3, bid: 2.9, ask: 3.1, close: 3.2 } as PriceData,
    });
    expect(draft.payload.action).toBe("BUY");
    expect(draft.payload.quantity).toBe(2);
    if (draft.payload.type === "option") {
      expect(draft.payload.right).toBe("P");
      expect(draft.payload.strike).toBe(180);
    }
  });
});

// `legPriceKey` is already imported in Task 2 — do not re-import.

function bullCallSpreadPos(): PortfolioPosition {
  // LONG $200C, SHORT $210C — net LONG (debit paid)
  return {
    id: 3,
    ticker: "SPY",
    structure: "Bull Call Spread",
    structure_type: "BullCallSpread",
    risk_profile: "defined",
    expiry: "2026-06-19",
    contracts: 4,
    direction: "LONG",
    entry_cost: 1200,
    max_risk: 1200,
    market_value: 1400,
    legs: [
      {
        direction: "LONG",
        contracts: 4,
        type: "Call",
        strike: 200,
        entry_cost: 1800,
        avg_cost: 4.5,
        market_price: 5,
        market_value: 2000,
        market_price_is_calculated: false,
      },
      {
        direction: "SHORT",
        contracts: 4,
        type: "Call",
        strike: 210,
        entry_cost: -600,
        avg_cost: 1.5,
        market_price: 1.5,
        market_value: -600,
        market_price_is_calculated: false,
      },
    ],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "",
  };
}

describe("buildCloseTicket — combo (bull call spread)", () => {
  const pos = bullCallSpreadPos();
  const expiry = "20260619";
  const prices: Record<string, PriceData> = {
    [legPriceKey("SPY", expiry, pos.legs[0])!]: {
      last: 5,
      bid: 4.9,
      ask: 5.1,
      close: 4.5,
    } as PriceData,
    [legPriceKey("SPY", expiry, pos.legs[1])!]: {
      last: 1.5,
      bid: 1.4,
      ask: 1.6,
      close: 1.5,
    } as PriceData,
  };

  it("produces combo payload with Order.action = SELL (closing LONG structure)", () => {
    const draft = buildCloseTicket(pos, prices);
    expect(draft.payload.type).toBe("combo");
    expect(draft.payload.action).toBe("SELL");
    expect(draft.payload.quantity).toBe(4);
  });

  it("per-leg ComboLeg.action stays LONG=BUY, SHORT=SELL regardless of Order.action", () => {
    // This is the load-bearing regression guard: flipping this causes IB error 201
    // (double-reversal). See web/CLAUDE.md → "IB Combo (BAG) Order Leg Convention".
    const draft = buildCloseTicket(pos, prices);
    if (draft.payload.type === "combo") {
      expect(draft.payload.legs).toHaveLength(2);
      const longLeg = draft.payload.legs.find((l) => l.strike === 200)!;
      const shortLeg = draft.payload.legs.find((l) => l.strike === 210)!;
      expect(longLeg.action).toBe("BUY"); // LONG leg → BUY
      expect(shortLeg.action).toBe("SELL"); // SHORT leg → SELL
      expect(longLeg.right).toBe("C");
      expect(shortLeg.right).toBe("C");
      expect(longLeg.ratio).toBe(1);
      expect(shortLeg.ratio).toBe(1);
    }
  });

  it("ratio spread — per-leg ratio = leg.contracts / position.contracts", () => {
    // 1x2: LONG 1 × $200C, SHORT 2 × $210C. position.contracts = 1.
    const ratioSpread: PortfolioPosition = {
      ...pos,
      contracts: 1,
      legs: [
        { ...pos.legs[0], contracts: 1 },
        { ...pos.legs[1], contracts: 2 },
      ],
    };
    const draft = buildCloseTicket(ratioSpread, prices);
    if (draft.payload.type === "combo") {
      expect(draft.payload.quantity).toBe(1);
      expect(draft.payload.legs.find((l) => l.strike === 200)!.ratio).toBe(1);
      expect(draft.payload.legs.find((l) => l.strike === 210)!.ratio).toBe(2);
    }
  });

  it("rejects covered-call / collar structures that include a Stock leg", () => {
    const coveredCall: PortfolioPosition = {
      ...pos,
      structure: "Covered Call",
      structure_type: "CoveredCall",
      legs: [
        {
          direction: "LONG",
          contracts: 100,
          type: "Stock",
          strike: null,
          entry_cost: 30000,
          avg_cost: 300,
          market_price: 305,
          market_value: 30500,
          market_price_is_calculated: false,
        },
        { ...pos.legs[1] },
      ],
    };
    expect(() => buildCloseTicket(coveredCall, prices)).toThrow(
      /stock\+option/i,
    );
  });

  it("net direction derived from leg signs, not P&L", () => {
    // Invert: SHORT the spread (credit spread). Order.action should become BUY to close.
    const shortSpread: PortfolioPosition = {
      ...pos,
      direction: "SHORT",
      legs: [
        { ...pos.legs[0], direction: "SHORT" },
        { ...pos.legs[1], direction: "LONG" },
      ],
    };
    const draft = buildCloseTicket(shortSpread, prices);
    expect(draft.payload.action).toBe("BUY");
    if (draft.payload.type === "combo") {
      const leg200 = draft.payload.legs.find((l) => l.strike === 200)!;
      const leg210 = draft.payload.legs.find((l) => l.strike === 210)!;
      expect(leg200.action).toBe("SELL"); // SHORT leg → SELL
      expect(leg210.action).toBe("BUY"); // LONG leg → BUY
    }
  });
});
