/**
 * TDD test: Frozen/stale LAST tick for options should not override real-time bid/ask.
 *
 * Bug: AAOI Mar 20 $105C shows Last Price = $25.26, but actual market is
 * bid=$10.30 ask=$11.70 (mid=$11.00). The $25.26 is yesterday's close/last trade,
 * sent by IB as a frozen LAST tick before live data arrives. Since the LAST tick
 * has a valid positive value, normalizeNumber accepts it, and it persists even
 * after bid/ask update to reflect reality.
 *
 * Fix: After processing each tick, if we have valid bid AND ask, and the current
 * `last` equals `close` (stale frozen value), recalculate `last` from the mid
 * of bid/ask which reflects current market conditions.
 *
 * Red/green TDD:
 * 1. These tests must FAIL before the fix (RED)
 * 2. Fix updatePriceFromTickPrice to detect stale last
 * 3. Tests pass (GREEN)
 */

import { describe, it, expect } from "vitest";

const handlerPath = new URL("../../scripts/ib_tick_handler.js", import.meta.url).pathname;
const { createPriceData, updatePriceFromTickPrice } = await import(handlerPath);

// IB TICK_TYPE constants
const TICK = {
  BID: 1, ASK: 2, LAST: 4, HIGH: 6, LOW: 7, CLOSE: 9,
  DELAYED_BID: 66, DELAYED_ASK: 67, DELAYED_LAST: 68, DELAYED_CLOSE: 75,
};

describe("Stale frozen LAST override — AAOI bug", () => {
  it("live LAST matching CLOSE is overridden by bid/ask midpoint when bid/ask diverge", () => {
    // Simulate IB tick sequence for AAOI option with reqMarketDataType(4):
    // CLOSE=25.26 (yesterday), LAST=25.26 (frozen/stale), BID=10.30, ASK=11.70
    const d = createPriceData("AAOI_20260320_105_C");

    // IB sends CLOSE first
    updatePriceFromTickPrice(d, TICK.CLOSE, 25.26);
    expect(d.close).toBe(25.26);

    // IB sends frozen LAST = same as close (stale)
    updatePriceFromTickPrice(d, TICK.LAST, 25.26);

    // IB sends real-time BID
    updatePriceFromTickPrice(d, TICK.BID, 10.30);

    // IB sends real-time ASK
    updatePriceFromTickPrice(d, TICK.ASK, 11.70);

    // After bid/ask arrive, last should reflect current market, not stale close
    expect(d.last).toBeCloseTo(11.0, 1); // mid of 10.30 and 11.70
    expect(d.lastIsCalculated).toBe(true);
  });

  it("does NOT override last when last differs from close (genuine trade)", () => {
    const d = createPriceData("AAOI_20260320_105_C");

    updatePriceFromTickPrice(d, TICK.CLOSE, 25.26);
    updatePriceFromTickPrice(d, TICK.LAST, 11.95); // genuine trade, differs from close
    updatePriceFromTickPrice(d, TICK.BID, 10.30);
    updatePriceFromTickPrice(d, TICK.ASK, 11.70);

    // last=11.95 is a genuine trade, NOT stale — keep it
    expect(d.last).toBe(11.95);
    expect(d.lastIsCalculated).toBe(false);
  });

  it("handles delayed tick sequence (DELAYED_LAST = DELAYED_CLOSE)", () => {
    const d = createPriceData("AAOI_20260320_105_C");

    updatePriceFromTickPrice(d, TICK.DELAYED_CLOSE, 25.26);
    updatePriceFromTickPrice(d, TICK.DELAYED_LAST, 25.26); // stale
    updatePriceFromTickPrice(d, TICK.DELAYED_BID, 10.30);
    updatePriceFromTickPrice(d, TICK.DELAYED_ASK, 11.70);

    expect(d.last).toBeCloseTo(11.0, 1);
    expect(d.lastIsCalculated).toBe(true);
  });

  it("BID/ASK arrive before LAST — stale LAST is corrected immediately", () => {
    const d = createPriceData("AAOI_20260320_105_C");

    updatePriceFromTickPrice(d, TICK.BID, 10.30);
    updatePriceFromTickPrice(d, TICK.ASK, 11.70);
    // At this point, last=mid=11.0 (calculated from bid/ask)

    // Now close arrives
    updatePriceFromTickPrice(d, TICK.CLOSE, 25.26);
    // Last should still be 11.0, not overridden by close
    expect(d.last).toBeCloseTo(11.0, 1);

    // Frozen LAST arrives = close
    updatePriceFromTickPrice(d, TICK.LAST, 25.26);
    // Should be corrected to midpoint since it matches stale close
    expect(d.last).toBeCloseTo(11.0, 1);
    expect(d.lastIsCalculated).toBe(true);
  });

  it("does NOT override for stocks where last=close is normal after hours", () => {
    // Stock tickers don't have _ in symbol name
    const d = createPriceData("AAOI");

    updatePriceFromTickPrice(d, TICK.CLOSE, 109.75);
    updatePriceFromTickPrice(d, TICK.LAST, 109.75);
    updatePriceFromTickPrice(d, TICK.BID, 109.50);
    updatePriceFromTickPrice(d, TICK.ASK, 110.00);

    // For stocks, last=close is normal (especially after hours) — don't override
    expect(d.last).toBe(109.75);
    expect(d.lastIsCalculated).toBe(false);
  });

  it("large divergence threshold: only override when bid/ask mid is >20% from last", () => {
    const d = createPriceData("AAOI_20260320_105_C");

    updatePriceFromTickPrice(d, TICK.CLOSE, 10.00);
    updatePriceFromTickPrice(d, TICK.LAST, 10.00);  // stale
    updatePriceFromTickPrice(d, TICK.BID, 9.80);
    updatePriceFromTickPrice(d, TICK.ASK, 10.20);

    // Mid=10.0, close=10.0, last=10.0 — within 20% → NO override needed
    // last should stay as 10.00 since there's no meaningful divergence
    expect(d.last).toBe(10.00);
  });

  it("correctly recalculates after genuine LAST tick arrives later", () => {
    const d = createPriceData("AAOI_20260320_105_C");

    // Stale sequence
    updatePriceFromTickPrice(d, TICK.CLOSE, 25.26);
    updatePriceFromTickPrice(d, TICK.LAST, 25.26);
    updatePriceFromTickPrice(d, TICK.BID, 10.30);
    updatePriceFromTickPrice(d, TICK.ASK, 11.70);
    expect(d.last).toBeCloseTo(11.0, 1); // corrected to mid

    // Now a genuine live LAST arrives
    updatePriceFromTickPrice(d, TICK.LAST, 11.95);
    expect(d.last).toBe(11.95);
    expect(d.lastIsCalculated).toBe(false);
  });
});
