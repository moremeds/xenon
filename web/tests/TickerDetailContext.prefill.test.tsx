// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  TickerDetailProvider,
  useTickerDetail,
} from "@/lib/TickerDetailContext";

describe("TickerDetailContext orderPrefill", () => {
  it("setOrderPrefill stamps a monotonic nonce", () => {
    const { result } = renderHook(() => useTickerDetail(), {
      wrapper: TickerDetailProvider,
    });
    act(() =>
      result.current.setOrderPrefill({
        price: 100,
        action: "BUY",
        source: "montage",
      }),
    );
    const n1 = result.current.orderPrefill!.nonce;
    act(() =>
      result.current.setOrderPrefill({
        price: 100,
        action: "BUY",
        source: "montage",
      }),
    );
    expect(result.current.orderPrefill!.nonce).toBeGreaterThan(n1);
  });

  it("setOrderPrefill carries price/action/source through", () => {
    const { result } = renderHook(() => useTickerDetail(), {
      wrapper: TickerDetailProvider,
    });
    act(() =>
      result.current.setOrderPrefill({
        price: 123.45,
        action: "SELL",
        quantity: 3,
        source: "tape",
      }),
    );
    expect(result.current.orderPrefill).toMatchObject({
      price: 123.45,
      action: "SELL",
      quantity: 3,
      source: "tape",
    });
  });
});
