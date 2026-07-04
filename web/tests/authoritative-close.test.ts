/**
 * The IB streaming market-data plane can freeze a contract's line across a
 * session boundary (error-101 starvation), so it keeps emitting a stale CLOSE/
 * LAST. IB's historical plane is authoritative. applyAuthoritativeClose lets a
 * freshly-fetched daily bar close override the frozen streaming value.
 *
 * Fixtures are REAL TSLA values pulled from the IB Gateway historical plane on
 * 2026-07-04 (as-of 2026-07-02 session):
 *   - frozen streaming line:  last=423.60, close=425.30  (stuck at Jul-1)
 *   - authoritative daily close (Jul-2 session): 393.45
 */
import { describe, it, expect } from "vitest";

const handlerPath = new URL(
  "../../scripts/infra/ib_realtime/ib_tick_handler.js",
  import.meta.url,
).pathname;
const { createPriceData, applyAuthoritativeClose } = await import(handlerPath);

const TSLA_JUL1_CLOSE = 425.3; // frozen streaming close
const TSLA_JUL1_LAST = 423.6; // frozen streaming last
const TSLA_JUL2_CLOSE = 393.45; // authoritative daily close

function frozenTsla() {
  const d = createPriceData("TSLA");
  d.last = TSLA_JUL1_LAST;
  d.close = TSLA_JUL1_CLOSE;
  d.lastIsCalculated = false;
  return d;
}

describe("applyAuthoritativeClose", () => {
  it("overrides a frozen streaming last+close when the line is stale", () => {
    const d = frozenTsla();
    const changed = applyAuthoritativeClose(d, TSLA_JUL2_CLOSE, true);
    expect(changed).toBe(true);
    expect(d.last).toBe(TSLA_JUL2_CLOSE);
    expect(d.close).toBe(TSLA_JUL2_CLOSE);
    expect(d.lastIsCalculated).toBe(true);
  });

  it("fills last from daily close when last is absent (closed market)", () => {
    const d = createPriceData("META");
    d.last = null;
    d.close = 612.91; // frozen Jul-1
    const changed = applyAuthoritativeClose(d, 582.9, false);
    expect(changed).toBe(true);
    expect(d.last).toBe(582.9);
    expect(d.close).toBe(582.9);
  });

  it("keeps a genuine live last, correcting only the stale close", () => {
    const d = createPriceData("TSLA");
    d.last = TSLA_JUL2_CLOSE; // live print (real Jul-2 value), not stale/calc
    d.lastIsCalculated = false;
    d.close = TSLA_JUL1_CLOSE; // streaming close lagged a session
    const changed = applyAuthoritativeClose(d, TSLA_JUL2_CLOSE, false);
    expect(changed).toBe(true);
    expect(d.last).toBe(TSLA_JUL2_CLOSE); // untouched
    expect(d.lastIsCalculated).toBe(false); // still a live print
    expect(d.close).toBe(TSLA_JUL2_CLOSE); // corrected
  });

  it("overrides a bid/ask-derived (calculated) last", () => {
    const d = createPriceData("TSLA");
    d.last = 410; // midpoint guess
    d.lastIsCalculated = true;
    d.close = TSLA_JUL1_CLOSE;
    applyAuthoritativeClose(d, TSLA_JUL2_CLOSE, false);
    expect(d.last).toBe(TSLA_JUL2_CLOSE);
  });

  it("returns false (no-op) when already at the authoritative close", () => {
    const d = createPriceData("TSLA");
    d.last = TSLA_JUL2_CLOSE;
    d.close = TSLA_JUL2_CLOSE;
    d.lastIsCalculated = true;
    expect(applyAuthoritativeClose(d, TSLA_JUL2_CLOSE, true)).toBe(false);
  });

  it("ignores options (they use the option close cache)", () => {
    const d = createPriceData("TSLA_20260717_400_C");
    d.last = 5.2;
    d.close = 5.5;
    expect(applyAuthoritativeClose(d, 393.45, true)).toBe(false);
    expect(d.close).toBe(5.5);
  });

  it("rejects a non-positive or non-numeric daily close", () => {
    const d = frozenTsla();
    expect(applyAuthoritativeClose(d, 0, true)).toBe(false);
    expect(applyAuthoritativeClose(d, -1, true)).toBe(false);
    expect(applyAuthoritativeClose(d, null, true)).toBe(false);
    expect(d.last).toBe(TSLA_JUL1_LAST); // untouched
  });
});
