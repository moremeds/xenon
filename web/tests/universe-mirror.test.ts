import { describe, expect, it } from "vitest";

import {
  UNIVERSE,
  INDEX_UNIVERSE,
  isKnown,
  isIndex,
  getMultiplier,
} from "../lib/universe";

describe("universe TS mirror", () => {
  it("contains exactly the nine V1 tickers", () => {
    expect(new Set(Object.keys(UNIVERSE))).toEqual(
      new Set(["SPX", "NDX", "RUT", "SPY", "QQQ", "IWM", "GLD", "USO", "SIL"]),
    );
  });

  it("has SPX/NDX/RUT in INDEX_UNIVERSE", () => {
    expect(INDEX_UNIVERSE).toEqual(new Set(["NDX", "RUT", "SPX"]));
  });

  it.each(["SPX", "NDX", "RUT"] as const)(
    "%s is cash-settled index with multiplier 100",
    (ticker) => {
      const e = UNIVERSE[ticker];
      expect(e.isIndex).toBe(true);
      expect(e.cashSettled).toBe(true);
      expect(e.multiplier).toBe(100);
      expect(e.type).toBe("INDEX");
    },
  );

  it.each(["SPY", "QQQ", "IWM", "GLD", "USO", "SIL"] as const)(
    "%s is ETF, deliverable, multiplier 100",
    (ticker) => {
      const e = UNIVERSE[ticker];
      expect(e.isIndex).toBe(false);
      expect(e.cashSettled).toBe(false);
      expect(e.multiplier).toBe(100);
      expect(e.type).toBe("ETF");
    },
  );

  it("USO is flagged k1, others are not", () => {
    expect(UNIVERSE.USO.k1).toBe(true);
    for (const t of [
      "SPX",
      "NDX",
      "RUT",
      "SPY",
      "QQQ",
      "IWM",
      "GLD",
      "SIL",
    ] as const) {
      expect(UNIVERSE[t].k1).toBe(false);
    }
  });

  it("isKnown returns true for V1 tickers and false for others", () => {
    expect(isKnown("SPX")).toBe(true);
    expect(isKnown("AAPL")).toBe(false);
  });

  it("isIndex throws for unknown ticker", () => {
    expect(() => isIndex("AAPL")).toThrow();
  });

  it("getMultiplier throws for unknown ticker", () => {
    expect(() => getMultiplier("AAPL")).toThrow();
  });
});
