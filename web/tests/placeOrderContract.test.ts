import { describe, it, expect } from "vitest";
import { buildFastApiPlaceOrderPayload } from "@/lib/order/placeOrderContract";

// Real IB contracts (2026-06-22): 5016 TSEJ/JPY, AAPL SMART/USD.
describe("buildFastApiPlaceOrderPayload — foreign stock venue/currency", () => {
  it("forwards exchange + currency for a foreign stock order", () => {
    const out = buildFastApiPlaceOrderPayload({
      type: "stock",
      symbol: "5016",
      action: "BUY",
      quantity: 100,
      limitPrice: 1,
      tif: "DAY",
      exchange: "TSEJ",
      currency: "JPY",
      client_attempt_id: "x",
    } as never);
    expect(out.exchange).toBe("TSEJ");
    expect(out.currency).toBe("JPY");
  });

  it("omits exchange/currency for a normal US stock order", () => {
    const out = buildFastApiPlaceOrderPayload({
      type: "stock",
      symbol: "AAPL",
      action: "BUY",
      quantity: 10,
      limitPrice: 295.27,
      tif: "DAY",
      client_attempt_id: "x",
    } as never);
    expect("exchange" in out).toBe(false);
    expect("currency" in out).toBe(false);
  });
});
