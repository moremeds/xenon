import { describe, it, expect } from "vitest";
import {
  computeSubscriptionDiff,
  subscriptionHash,
  type DesiredSubscriptions,
} from "@/lib/usePrices";

const empty: DesiredSubscriptions = {
  symbols: [],
  contracts: [],
  indexes: [],
  forexes: [],
  stocksMeta: [],
};

const desired = (
  over: Partial<DesiredSubscriptions>,
): DesiredSubscriptions => ({
  ...empty,
  ...over,
});

describe("subscriptionHash", () => {
  it("always emits 5 '|'-delimited segments (lockstep with the diff parser)", () => {
    expect(subscriptionHash(empty).split("|")).toHaveLength(5);
    expect(
      subscriptionHash(
        desired({
          symbols: ["AAPL"],
          forexes: [{ base: "USD", quote: "JPY" }],
          stocksMeta: [{ symbol: "5016", exchange: "TSEJ", currency: "JPY" }],
        }),
      ).split("|"),
    ).toHaveLength(5);
  });
});

describe("computeSubscriptionDiff — forex + foreign stocks", () => {
  it("adds a forex pair and a foreign stock from an empty baseline", () => {
    const diff = computeSubscriptionDiff(
      "",
      desired({
        forexes: [{ base: "USD", quote: "JPY" }],
        stocksMeta: [{ symbol: "5016", exchange: "TSEJ", currency: "JPY" }],
      }),
    );
    expect(diff.changed).toBe(true);
    expect(diff.addedForexes).toEqual([{ base: "USD", quote: "JPY" }]);
    expect(diff.addedStocks).toEqual([
      { symbol: "5016", exchange: "TSEJ", currency: "JPY" },
    ]);
    expect(diff.removedForexKeys).toEqual([]);
    expect(diff.removedStockSymbols).toEqual([]);
  });

  it("removes a forex pair when it drops out of the desired set", () => {
    // last-sent had USD.JPY (segment 4); desired no longer wants it.
    const lastHash = subscriptionHash(
      desired({ forexes: [{ base: "USD", quote: "JPY" }] }),
    );
    const diff = computeSubscriptionDiff(lastHash, empty);
    expect(diff.removedForexKeys).toEqual(["USD.JPY"]);
    expect(diff.addedForexes).toEqual([]);
  });

  it("removes a foreign stock by bare symbol", () => {
    const lastHash = subscriptionHash(
      desired({
        stocksMeta: [{ symbol: "000660", exchange: "KRX", currency: "KRW" }],
      }),
    );
    const diff = computeSubscriptionDiff(lastHash, empty);
    expect(diff.removedStockSymbols).toEqual(["000660"]);
    expect(diff.addedStocks).toEqual([]);
  });

  it("is a no-op when desired equals last-sent (forex + stock present)", () => {
    const d = desired({
      symbols: ["AAPL"],
      forexes: [{ base: "USD", quote: "KRW" }],
      stocksMeta: [{ symbol: "000660", exchange: "KRX", currency: "KRW" }],
    });
    const diff = computeSubscriptionDiff(subscriptionHash(d), d);
    expect(diff.changed).toBe(false);
    expect(diff.addedForexes).toEqual([]);
    expect(diff.addedStocks).toEqual([]);
    expect(diff.removedForexKeys).toEqual([]);
    expect(diff.removedStockSymbols).toEqual([]);
  });

  it("does not cross-contaminate: adding a forex leaves symbols untouched", () => {
    // last-sent already had AAPL; now we ALSO want USD.JPY. AAPL must not be
    // re-subscribed or removed — proves the 5-segment split aligns correctly.
    const lastHash = subscriptionHash(desired({ symbols: ["AAPL"] }));
    const diff = computeSubscriptionDiff(
      lastHash,
      desired({ symbols: ["AAPL"], forexes: [{ base: "USD", quote: "JPY" }] }),
    );
    expect(diff.addedSymbols).toEqual([]);
    expect(diff.removedSymbols).toEqual([]);
    expect(diff.addedForexes).toEqual([{ base: "USD", quote: "JPY" }]);
  });
});
