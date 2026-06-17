import { describe, it, expect } from "vitest";
import {
  isOptionExpired,
  resolveBookSubject,
  positionBookKey,
} from "@/lib/book/bookSubject";
import type { PriceData } from "@/lib/pricesProtocol";
import type { PortfolioLeg, PortfolioPosition } from "@/lib/types";

const px = (symbol: string, last: number): PriceData =>
  ({ symbol, last, bid: last - 0.05, ask: last + 0.05 }) as PriceData;

const OPT = "QQQ_20260717_692_P";

describe("isOptionExpired", () => {
  it("is false on the expiry date itself (still exercisable that day)", () => {
    expect(isOptionExpired("20260717", "20260717")).toBe(false);
  });
  it("is true the day after expiry", () => {
    expect(isOptionExpired("20260717", "20260718")).toBe(true);
  });
  it("is false well before expiry", () => {
    expect(isOptionExpired("20260717", "20260617")).toBe(false);
  });
  it("is false for a malformed expiry (never auto-hide on bad data)", () => {
    expect(isOptionExpired("nope", "20260717")).toBe(false);
  });
});

describe("resolveBookSubject", () => {
  const prices = { QQQ: px("QQQ", 733.7), [OPT]: px(OPT, 5.78) };
  const base = {
    ticker: "QQQ",
    leg: null as string | null,
    hasPosId: false,
    positionOptionKey: null as string | null,
    positionPriceData: null as PriceData | null,
    prices,
    todayYmd: "20260617",
  };

  it("bare ticker (no leg, no posId) → STOCK book on the underlying", () => {
    const s = resolveBookSubject(base);
    expect(s.bookKind).toBe("stock");
    expect(s.bookKey).toBe("QQQ");
    expect(s.quotePriceData?.last).toBe(733.7);
  });

  it("bare ticker stays STOCK even when a position exists by ticker", () => {
    // position present but NOT explicitly selected (no posId) → underlying
    const s = resolveBookSubject({
      ...base,
      positionOptionKey: OPT,
      positionPriceData: px(OPT, 5.78),
    });
    expect(s.bookKind).toBe("stock");
    expect(s.bookKey).toBe("QQQ");
  });

  it("?leg= → OPTION book for that contract", () => {
    const s = resolveBookSubject({ ...base, leg: OPT });
    expect(s.bookKind).toBe("option");
    expect(s.bookKey).toBe(OPT);
    expect(s.quotePriceData?.last).toBe(5.78);
  });

  it("posId-selected single-leg option → OPTION book (existing nav preserved)", () => {
    const s = resolveBookSubject({
      ...base,
      hasPosId: true,
      positionOptionKey: OPT,
      positionPriceData: px(OPT, 5.78),
    });
    expect(s.bookKind).toBe("option");
    expect(s.bookKey).toBe(OPT);
    // reuses the position quote (carries the calculated-mark fallback)
    expect(s.quotePriceData?.last).toBe(5.78);
  });

  it("expired leg falls back to the STOCK book (auto-remove dead option)", () => {
    const s = resolveBookSubject({
      ...base,
      leg: OPT,
      todayYmd: "20260718", // day after 2026-07-17
    });
    expect(s.bookKind).toBe("stock");
    expect(s.bookKey).toBe("QQQ");
  });

  it("a leg for a different underlying is ignored → STOCK book", () => {
    const s = resolveBookSubject({ ...base, leg: "SPY_20260717_500_C" });
    expect(s.bookKind).toBe("stock");
    expect(s.bookKey).toBe("QQQ");
  });

  it("?leg= wins over the posId option when both are present", () => {
    const other = "QQQ_20260717_700_C";
    const s = resolveBookSubject({
      ...base,
      leg: other,
      hasPosId: true,
      positionOptionKey: OPT,
      positionPriceData: px(OPT, 5.78),
      prices: { ...prices, [other]: px(other, 3.2) },
    });
    expect(s.bookKey).toBe(other);
    expect(s.quotePriceData?.last).toBe(3.2);
  });

  it("canonicalizes a non-canonical ?leg= (dashed expiry) so the quote resolves", () => {
    // A dashed-expiry leg parses to the same contract but a different raw string.
    // bookKey must be the canonical optionKey, else prices[bookKey] (keyed by the
    // canonical form) misses and the book renders live-looking but quote-less.
    const s = resolveBookSubject({ ...base, leg: "QQQ_2026-07-17_692_P" });
    expect(s.bookKind).toBe("option");
    expect(s.bookKey).toBe(OPT); // canonical, NOT "QQQ_2026-07-17_692_P"
    expect(s.quotePriceData?.last).toBe(5.78);
  });
});

describe("positionBookKey", () => {
  const optLeg: PortfolioLeg = {
    direction: "LONG",
    contracts: 1,
    type: "Put",
    strike: 692,
    entry_cost: 0,
    avg_cost: 0,
    market_price: null, // no live/marked quote — the regression scenario
    market_value: null,
  };
  const mkPos = (over: Partial<PortfolioPosition>): PortfolioPosition =>
    ({
      id: 1,
      ticker: "QQQ",
      structure: "Long Put",
      structure_type: "Long Put",
      risk_profile: "",
      expiry: "20260717",
      contracts: 1,
      direction: "LONG",
      entry_cost: 0,
      max_risk: null,
      market_value: null,
      legs: [optLeg],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "",
      ...over,
    }) as PortfolioPosition;

  it("derives the option key for a single-leg option EVEN with no quote (market_price null)", () => {
    expect(positionBookKey(mkPos({}))).toBe("QQQ_20260717_692_P");
  });
  it("returns null for a stock position", () => {
    expect(
      positionBookKey(
        mkPos({
          structure_type: "Stock",
          legs: [{ ...optLeg, type: "Stock", strike: null }],
        }),
      ),
    ).toBeNull();
  });
  it("returns null for a multi-leg position", () => {
    expect(positionBookKey(mkPos({ legs: [optLeg, optLeg] }))).toBeNull();
  });
  it("returns null when there is no position", () => {
    expect(positionBookKey(null)).toBeNull();
  });
});
