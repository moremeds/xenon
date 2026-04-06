import { describe, expect, it } from "vitest";
import { applyRestingLimitToQuote } from "@/lib/modifyOrderQuote";
import type { PriceData } from "@/lib/pricesProtocol";

const BASE_QUOTE: PriceData = {
  symbol: "AAOI_20260327_90_P",
  bid: 4.3,
  ask: 5.1,
  last: 4.8,
  timestamp: new Date().toISOString(),
};

describe("applyRestingLimitToQuote", () => {
  it("uses a resting sell limit as the effective ask when it improves the displayed market", () => {
    const quote = applyRestingLimitToQuote({
      priceData: BASE_QUOTE,
      action: "SELL",
      limitPrice: 5.05,
    });

    expect(quote).toMatchObject({ bid: 4.3, ask: 5.05, last: 4.8 });
  });

  it("uses a resting buy limit as the effective bid when it improves the displayed market", () => {
    const quote = applyRestingLimitToQuote({
      priceData: BASE_QUOTE,
      action: "BUY",
      limitPrice: 4.35,
    });

    expect(quote).toMatchObject({ bid: 4.35, ask: 5.1, last: 4.8 });
  });

  it("keeps a better market ask when the resting sell limit is less competitive", () => {
    const quote = applyRestingLimitToQuote({
      priceData: BASE_QUOTE,
      action: "SELL",
      limitPrice: 5.25,
    });

    expect(quote).toMatchObject({ bid: 4.3, ask: 5.1, last: 4.8 });
  });
});
