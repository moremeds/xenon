/** @vitest-environment jsdom */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import React from "react";
import {
  OrderActionsProvider,
  useOrderActions,
} from "@/lib/OrderActionsContext";
import {
  FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
  futuCancelApiPath,
  futuModifyApiPath,
  futuPlaceApiPath,
} from "@/lib/futuOpenOrder";
import { submitPlaceOrder } from "@/lib/order/submitPlace";
import { isUncertainPlaceResponse } from "@/lib/order/submitPlace";
import { ORDER_REASON_CODES } from "@/lib/orderReasonCodes";
import { futuTradingEnabledForEnv } from "@/lib/useFutuTrading";
import type { OpenOrder } from "@/lib/types";

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(OrderActionsProvider, null, children);
}

const futuOrder: OpenOrder = {
  orderId: 0,
  permId: 0,
  brokerOrderId: "9001",
  broker: "FUTU",
  accountEnv: "paper",
  orderType: "LMT",
  symbol: "AAPL",
  contract: {
    conId: null,
    symbol: "AAPL",
    secType: "STK",
    strike: null,
    right: null,
    expiry: null,
  },
  action: "SELL",
  totalQuantity: 1,
  limitPrice: 100,
  auxPrice: null,
  status: "Submitted",
  filled: 0,
  remaining: 1,
  avgFillPrice: null,
  tif: "DAY",
  modifySequence: 1,
};

describe("futu order scope helpers", () => {
  it("builds scoped API paths", () => {
    expect(futuPlaceApiPath("paper")).toBe(
      "/api/orders/place?broker=FUTU&account_env=paper",
    );
    expect(futuCancelApiPath("live")).toBe(
      "/api/orders/cancel?broker=FUTU&account_env=live",
    );
    expect(futuModifyApiPath("paper")).toContain("account_env=paper");
  });

  it("gates trading per env from health status", () => {
    expect(
      futuTradingEnabledForEnv({ paper: true, live: false }, "paper"),
    ).toBe(true);
    expect(futuTradingEnabledForEnv({ paper: true, live: false }, "live")).toBe(
      false,
    );
    expect(futuTradingEnabledForEnv(null, "paper")).toBe(false);
  });
});

describe("FUTU_SEND_UNCERTAIN", () => {
  it("locks place flow like PLACE_OUTCOME_UNKNOWN", () => {
    expect(
      isUncertainPlaceResponse(502, { reason_code: "FUTU_SEND_UNCERTAIN" }),
    ).toBe(true);
    expect(ORDER_REASON_CODES.FUTU_SEND_UNCERTAIN.copy).toMatch(
      /Do not resend/,
    );
  });
});

describe("OrderActionsContext Futu cancel", () => {
  const fetchSpy = vi.fn();

  beforeEach(() => {
    fetchSpy.mockReset();
    vi.stubGlobal("fetch", fetchSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("POST cancel includes brokerOrderId and scoped query", async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: "ok", orders: { open_orders: [] } }),
    });

    const { result } = renderHook(() => useOrderActions(), { wrapper });
    await act(async () => {
      await result.current.requestCancel(futuOrder);
    });

    expect(fetchSpy).toHaveBeenCalled();
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(futuCancelApiPath("paper"));
    expect(JSON.parse(String(init.body))).toEqual({ brokerOrderId: "9001" });
  });
});

describe("OrderActionsContext Futu modify confirmed", () => {
  const fetchSpy = vi.fn();

  beforeEach(() => {
    fetchSpy.mockReset();
    vi.stubGlobal("fetch", fetchSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("confirmed true → success toast, no refresh poll", async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        status: "ok",
        confirmed: true,
        applied_sequence: 2,
        orders: { open_orders: [], executed_orders: [] },
      }),
    });

    const { result } = renderHook(() => useOrderActions(), { wrapper });
    await act(async () => {
      await result.current.requestModify(futuOrder, { newPrice: 101 });
    });

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const notes = result.current.drainNotifications();
    expect(
      notes.some((n) => n.type === "success" && n.message.includes("101")),
    ).toBe(true);
    expect(result.current.isPendingModify(futuOrder)).toBe(false);
  });

  it("confirmed false → warning copy, no IB-style error on poll timeout", async () => {
    vi.useFakeTimers();
    fetchSpy
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          status: "ok",
          confirmed: false,
          applied_sequence: 2,
        }),
      })
      .mockResolvedValue({
        ok: true,
        json: async () => ({
          last_sync: new Date().toISOString(),
          open_orders: [
            {
              ...futuOrder,
              limitPrice: 100,
            },
          ],
          executed_orders: [],
          open_count: 1,
          executed_count: 0,
        }),
      });

    const { result } = renderHook(() => useOrderActions(), { wrapper });
    await act(async () => {
      await result.current.requestModify(futuOrder, { newPrice: 101 });
    });

    const notes = result.current.drainNotifications();
    expect(
      notes.some((n) => n.message === FUTU_MODIFY_SENT_UNCONFIRMED_COPY),
    ).toBe(true);

    await act(async () => {
      vi.advanceTimersByTime(5_000 * 25);
    });

    const after = result.current.drainNotifications();
    expect(after.some((n) => n.type === "error")).toBe(false);
  });
});

describe("submitPlaceOrder Futu scope", () => {
  it("appends scope query on place URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "ok", brokerOrderId: "1" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const attempt = {
      id: "test",
      isUncertain: () => false,
      markSubmitted: () => undefined,
      markTerminal: () => undefined,
      markUncertain: () => undefined,
    };
    await submitPlaceOrder(
      attempt,
      {
        type: "stock",
        symbol: "AAPL",
        action: "BUY",
        quantity: 1,
        limitPrice: 1,
      },
      { scopeQuery: "?broker=FUTU&account_env=paper" },
    );
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/orders/place?broker=FUTU&account_env=paper",
    );
    vi.unstubAllGlobals();
  });
});
