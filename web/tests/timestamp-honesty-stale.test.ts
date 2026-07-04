/**
 * TDD: the relay's per-symbol `timestamp` must only advance when a tick actually
 * changed a value. On a closed market IB streams bid/ask = -1 (rejected to null
 * by normalizeNumber); those no-op ticks previously still bumped `data.timestamp`,
 * so a stale `last` (e.g. a pre-close cache) was emitted with a *fresh* timestamp
 * — a lying freshness signal that let downstream (argon) display stale prices.
 *
 * Fix: updatePriceFromTickPrice / updatePriceFromTickSize return `true` only when
 * a meaningful field changed, and bump `timestamp` only then.
 *
 * Root cause of the original report was IB line-budget starvation (error 102
 * "Max number of tickers"); this test locks in the freshness-signal half of the
 * fix so a starved symbol carries an honest (old) timestamp.
 */

import { describe, it, expect } from "vitest";

const handlerPath = new URL(
  "../../scripts/infra/ib_realtime/ib_tick_handler.js",
  import.meta.url,
).pathname;
const { createPriceData, updatePriceFromTickPrice, updatePriceFromTickSize } =
  await import(handlerPath);

const TICK = { BID: 1, ASK: 2, LAST: 4, VOLUME: 8, BID_SIZE: 0, ASK_SIZE: 3 };

describe("timestamp honesty", () => {
  it("returns true and advances timestamp on a real value change", () => {
    const d = createPriceData("TSLA");
    const before = d.timestamp;
    const changed = updatePriceFromTickPrice(d, TICK.LAST, 394.4);
    expect(changed).toBe(true);
    expect(d.last).toBe(394.4);
    // timestamp is reassigned (a fresh ISO string object), value >= before
    expect(d.timestamp >= before).toBe(true);
  });

  it("returns false and does NOT touch timestamp on a rejected (-1) quote", () => {
    const d = createPriceData("TSLA");
    d.timestamp = "2020-01-01T00:00:00.000Z"; // sentinel old timestamp
    const changed = updatePriceFromTickPrice(d, TICK.BID, -1);
    expect(changed).toBe(false);
    expect(d.bid).toBe(null);
    expect(d.timestamp).toBe("2020-01-01T00:00:00.000Z"); // unchanged
  });

  it("returns false when a LAST tick repeats the same value", () => {
    const d = createPriceData("TSLA");
    updatePriceFromTickPrice(d, TICK.LAST, 100);
    d.timestamp = "2020-01-01T00:00:00.000Z";
    const changed = updatePriceFromTickPrice(d, TICK.LAST, 100);
    expect(changed).toBe(false);
    expect(d.timestamp).toBe("2020-01-01T00:00:00.000Z");
  });

  it("tickSize returns false and preserves timestamp on a no-op size", () => {
    const d = createPriceData("TSLA");
    d.timestamp = "2020-01-01T00:00:00.000Z";
    const changed = updatePriceFromTickSize(d, TICK.VOLUME, -1);
    expect(changed).toBe(false);
    expect(d.timestamp).toBe("2020-01-01T00:00:00.000Z");
  });

  it("tickSize returns true and advances timestamp on a real volume", () => {
    const d = createPriceData("TSLA");
    d.timestamp = "2020-01-01T00:00:00.000Z";
    const changed = updatePriceFromTickSize(d, TICK.VOLUME, 53_600_000);
    expect(changed).toBe(true);
    expect(d.volume).toBe(53_600_000);
    expect(d.timestamp).not.toBe("2020-01-01T00:00:00.000Z");
  });
});
