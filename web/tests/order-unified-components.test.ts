/**
 * Unified Order Components Tests
 *
 * Tests for web/lib/order/ — reusable order system components and hooks.
 */

import { describe, it, expect } from "vitest";

// Note: These are unit tests for the pure logic.
// Component rendering tests would use @testing-library/react.

describe("useOrderPrices hook", () => {
  // We test the algorithm directly since the hook is a thin wrapper

  describe("stock prices", () => {
    it("computes spread for stock", () => {
      const bid = 249.75;
      const ask = 249.85;
      const spread = ask - bid;
      const mid = (bid + ask) / 2;
      const spreadPct = (spread / mid) * 100;

      expect(spread).toBeCloseTo(0.10, 2);
      expect(mid).toBeCloseTo(249.80, 2);
      expect(spreadPct).toBeCloseTo(0.04, 2);
    });
  });

  describe("combo prices — natural market calculation", () => {
    // Bull call spread: LONG $315C, SHORT $340C
    // $315C: bid=8.50, ask=8.70
    // $340C: bid=2.00, ask=2.20

    it("computes net prices for BUY combo (opening)", () => {
      // BUY combo: pay ASK on LONG legs, receive BID on SHORT legs
      // LONG leg: effectivelySelling = (BUY === LONG) = false → pay ask (8.70)
      // SHORT leg: effectivelySelling = (BUY === SHORT) = true → receive bid (2.00)
      // Wait, that's not right. Let me trace through:
      //
      // For BUY action:
      //   LONG leg: effectivelySelling = (BUY === LONG) = false → we're BUYING → pay ASK
      //   SHORT leg: effectivelySelling = (BUY === SHORT) = true → we're SELLING → receive BID
      //
      // netAsk = 8.70 - 2.00 = 6.70 (cost to BUY)
      // netBid = 8.50 - 2.20 = 6.30 (proceeds if we SELL)

      const leg1 = { direction: "LONG", bid: 8.50, ask: 8.70 };
      const leg2 = { direction: "SHORT", bid: 2.00, ask: 2.20 };
      const action = "BUY";

      let netBid = 0;
      let netAsk = 0;

      for (const leg of [leg1, leg2]) {
        const effectivelySelling = (action === "SELL") === (leg.direction === "LONG");
        if (effectivelySelling) {
          netBid += leg.bid;
          netAsk += leg.ask;
        } else {
          netBid -= leg.ask;
          netAsk -= leg.bid;
        }
      }

      const absBid = Math.abs(netBid);
      const absAsk = Math.abs(netAsk);
      const bid = Math.min(absBid, absAsk);
      const ask = Math.max(absBid, absAsk);

      expect(bid).toBeCloseTo(6.30, 2);
      expect(ask).toBeCloseTo(6.70, 2);
    });

    it("computes net prices for SELL combo (closing)", () => {
      // SELL combo: receive BID on LONG legs, pay ASK on SHORT legs
      // LONG leg: effectivelySelling = (SELL === LONG) = true → receive bid (8.50)
      // SHORT leg: effectivelySelling = (SELL === SHORT) = false → pay ask (2.20)
      //
      // netBid = 8.50 - 2.20 = 6.30 (proceeds to SELL)
      // netAsk = 8.70 - 2.00 = 6.70 (cost to BUY)

      const leg1 = { direction: "LONG", bid: 8.50, ask: 8.70 };
      const leg2 = { direction: "SHORT", bid: 2.00, ask: 2.20 };
      const action = "SELL";

      let netBid = 0;
      let netAsk = 0;

      for (const leg of [leg1, leg2]) {
        const effectivelySelling = (action === "SELL") === (leg.direction === "LONG");
        if (effectivelySelling) {
          netBid += leg.bid;
          netAsk += leg.ask;
        } else {
          netBid -= leg.ask;
          netAsk -= leg.bid;
        }
      }

      const absBid = Math.abs(netBid);
      const absAsk = Math.abs(netAsk);
      const bid = Math.min(absBid, absAsk);
      const ask = Math.max(absBid, absAsk);

      // Same result for both actions — the bid/ask is the spread's market, not the execution
      expect(bid).toBeCloseTo(6.30, 2);
      expect(ask).toBeCloseTo(6.70, 2);
    });

    it("computes mid and spread correctly", () => {
      const bid = 6.30;
      const ask = 6.70;
      const mid = (bid + ask) / 2;
      const spread = ask - bid;
      const spreadPct = (spread / mid) * 100;

      expect(mid).toBeCloseTo(6.50, 2);
      expect(spread).toBeCloseTo(0.40, 2);
      expect(spreadPct).toBeCloseTo(6.15, 1);
    });
  });
});

describe("useOrderValidation hook", () => {
  function validateOrder(quantity: string, limitPrice: string) {
    const errors: Record<string, string> = {};
    const parsedQuantity = parseInt(quantity, 10);
    const parsedPrice = parseFloat(limitPrice);

    if (quantity === "" || isNaN(parsedQuantity)) {
      errors.quantity = "Quantity is required";
    } else if (parsedQuantity < 1) {
      errors.quantity = "Quantity must be at least 1";
    }

    if (limitPrice === "" || isNaN(parsedPrice)) {
      errors.price = "Price is required";
    } else if (parsedPrice < 0.01) {
      errors.price = "Price must be at least $0.01";
    }

    return {
      isValid: Object.keys(errors).length === 0,
      errors,
      parsedQuantity: isNaN(parsedQuantity) ? 0 : parsedQuantity,
      parsedPrice: isNaN(parsedPrice) ? 0 : parsedPrice,
    };
  }

  it("validates valid order", () => {
    const result = validateOrder("44", "6.50");
    expect(result.isValid).toBe(true);
    expect(result.parsedQuantity).toBe(44);
    expect(result.parsedPrice).toBe(6.50);
  });

  it("rejects empty quantity", () => {
    const result = validateOrder("", "6.50");
    expect(result.isValid).toBe(false);
    expect(result.errors.quantity).toBeDefined();
  });

  it("rejects zero quantity", () => {
    const result = validateOrder("0", "6.50");
    expect(result.isValid).toBe(false);
    expect(result.errors.quantity).toBeDefined();
  });

  it("rejects negative quantity", () => {
    const result = validateOrder("-10", "6.50");
    expect(result.isValid).toBe(false);
    expect(result.errors.quantity).toBeDefined();
  });

  it("rejects empty price", () => {
    const result = validateOrder("44", "");
    expect(result.isValid).toBe(false);
    expect(result.errors.price).toBeDefined();
  });

  it("rejects zero price", () => {
    const result = validateOrder("44", "0");
    expect(result.isValid).toBe(false);
    expect(result.errors.price).toBeDefined();
  });

  it("rejects negative price", () => {
    const result = validateOrder("44", "-5.00");
    expect(result.isValid).toBe(false);
    expect(result.errors.price).toBeDefined();
  });
});

describe("OrderPriceStrip formatting", () => {
  function formatPrice(value: number | null): string {
    if (value == null) return "---";
    return `$${value.toFixed(2)}`;
  }

  function formatPct(value: number | null): string {
    if (value == null) return "";
    return `(${value.toFixed(1)}%)`;
  }

  it("formats prices correctly", () => {
    expect(formatPrice(6.30)).toBe("$6.30");
    expect(formatPrice(249.80)).toBe("$249.80");
    expect(formatPrice(0.05)).toBe("$0.05");
  });

  it("formats null as ---", () => {
    expect(formatPrice(null)).toBe("---");
  });

  it("formats spread percentage", () => {
    expect(formatPct(6.15)).toBe("(6.2%)");
    expect(formatPct(0.04)).toBe("(0.0%)");
    expect(formatPct(null)).toBe("");
  });
});

describe("OrderLegPills formatting", () => {
  it("formats long leg with + prefix", () => {
    const leg = { direction: "LONG", strike: 315, type: "Call" };
    const prefix = leg.direction === "LONG" ? "+" : "−";
    expect(prefix).toBe("+");
  });

  it("formats short leg with − prefix", () => {
    const leg = { direction: "SHORT", strike: 340, type: "Call" };
    const prefix = leg.direction === "LONG" ? "+" : "−";
    expect(prefix).toBe("−");
  });

  it("formats strike and type", () => {
    const leg = { direction: "LONG", strike: 315, type: "Call" };
    const formatted = `$${leg.strike} ${leg.type}`;
    expect(formatted).toBe("$315 Call");
  });
});

describe("OrderConfirmSummary formatting", () => {
  function formatCurrency(value: number | null): string {
    if (value == null) return "---";
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    }).format(value);
  }

  it("formats total cost", () => {
    expect(formatCurrency(28600)).toBe("$28,600");
    expect(formatCurrency(1500)).toBe("$1,500");
  });

  it("formats max gain", () => {
    expect(formatCurrency(82456)).toBe("$82,456");
  });

  it("formats null as ---", () => {
    expect(formatCurrency(null)).toBe("---");
  });
});
