import { describe, it, expect } from "vitest";
import {
  toUsd,
  usdPerUnitFromForexTick,
  fmtNative,
  deriveFxSubscriptions,
} from "@/lib/fx";

// Real IB snapshot, 2026-06-22:
//   5016 JX Advanced Metals: 100 sh @ ¥5,267 = ¥526,700 market value
//   USD.JPY (IDEALPRO) = 161.6575 JPY/USD → usd_per_unit[JPY] = 1/161.6575
//   USD.KRW (IDEALPRO) = 1538.505 KRW/USD
const JPY = 1 / 161.6575;

describe("toUsd", () => {
  it("is identity for USD", () =>
    expect(toUsd(1234, "USD", { USD: 1 })).toBe(1234));
  it("converts a JPY market value with the live rate", () =>
    expect(toUsd(526_700, "JPY", { USD: 1, JPY })!).toBeCloseTo(
      526_700 * JPY,
      6,
    ));
  it("returns null when the rate is missing", () =>
    expect(toUsd(2_885_000, "KRW", { USD: 1 })).toBeNull());
  it("returns null for null amount", () =>
    expect(toUsd(null, "JPY", { JPY })).toBeNull());
  it("defaults blank currency to USD identity", () =>
    expect(toUsd(50, "", { USD: 1 })).toBe(50));
});

describe("usdPerUnitFromForexTick", () => {
  it("inverts the JPY-per-USD tick into USD-per-JPY", () => {
    const r = usdPerUnitFromForexTick("USD.JPY", 161.6575)!;
    expect(r.currency).toBe("JPY");
    expect(r.rate).toBeCloseTo(1 / 161.6575, 10);
  });
  it("inverts the KRW-per-USD tick", () => {
    const r = usdPerUnitFromForexTick("USD.KRW", 1538.505)!;
    expect(r.currency).toBe("KRW");
    expect(r.rate).toBeCloseTo(1 / 1538.505, 10);
  });
  it("returns null for non-USD-base pairs or bad ticks", () => {
    expect(usdPerUnitFromForexTick("USD.JPY", 0)).toBeNull();
    expect(usdPerUnitFromForexTick("EUR.JPY", 1.2)).toBeNull();
    expect(usdPerUnitFromForexTick("USD.JPY", null)).toBeNull();
  });
});

describe("fmtNative", () => {
  it("formats JPY with no decimals", () => {
    expect(fmtNative(526_700, "JPY")).toContain("526,700");
  });
  it("formats KRW with no decimals", () => {
    expect(fmtNative(2_885_000, "KRW")).toContain("2,885,000");
  });
  it("returns a placeholder for null", () => {
    expect(fmtNative(null, "JPY")).toBe("---");
  });
});

describe("deriveFxSubscriptions", () => {
  it("splits USD vs foreign positions into the right subscription buckets", () => {
    const out = deriveFxSubscriptions([
      { ticker: "AAPL", currency: "USD", exchange: "SMART" },
      { ticker: "5016", currency: "JPY", exchange: "TSEJ" },
      { ticker: "000660", currency: "KRW", exchange: "KRX" },
    ]);
    expect(out.usdSymbols).toEqual(["AAPL"]);
    expect(out.forexes).toEqual([
      { base: "USD", quote: "JPY" },
      { base: "USD", quote: "KRW" },
    ]);
    expect(out.stocksMeta).toEqual([
      { symbol: "5016", exchange: "TSEJ", currency: "JPY" },
      { symbol: "000660", exchange: "KRX", currency: "KRW" },
    ]);
  });

  it("a non-USD position without an exchange still triggers a forex pair, no foreign quote", () => {
    const out = deriveFxSubscriptions([
      { ticker: "5016", currency: "JPY", exchange: null },
    ]);
    expect(out.usdSymbols).toEqual([]);
    expect(out.stocksMeta).toEqual([]);
    expect(out.forexes).toEqual([{ base: "USD", quote: "JPY" }]);
  });

  it("defaults missing currency to USD", () => {
    const out = deriveFxSubscriptions([{ ticker: "SPY" }]);
    expect(out.usdSymbols).toEqual(["SPY"]);
    expect(out.forexes).toEqual([]);
  });
});
