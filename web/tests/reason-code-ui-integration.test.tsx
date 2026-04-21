/**
 * @vitest-environment jsdom
 *
 * C3 — end-to-end readReasonCode() helper exercise through
 * OrderActionsContext. The existing `orders-upstream-preserved.test.ts` is
 * route-only (no UI mount), so these assertions live here as a companion
 * file that exercises the full UI path: fetch response → Next.js body shape
 * → readReasonCode → getReasonToast → pushNotification → ActionResult.
 */
import { describe, test, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderHook, act } from "@testing-library/react";
import {
  OrderActionsProvider,
  useOrderActions,
} from "@/lib/OrderActionsContext";
import { ORDER_REASON_CODES } from "@/lib/orderReasonCodes";
import type { OpenOrder } from "@/lib/types";

const wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(OrderActionsProvider, null, children);

function makeOrder(): OpenOrder {
  return {
    orderId: 101,
    permId: 9001,
    symbol: "SPY",
    contract: {
      conId: 1,
      symbol: "SPY",
      secType: "STK",
      strike: null,
      right: null,
      expiry: null,
    },
    action: "SELL",
    orderType: "LMT",
    totalQuantity: 1,
    limitPrice: 500,
    auxPrice: null,
    status: "Submitted",
    filled: 0,
    remaining: 1,
    avgFillPrice: null,
    tif: "DAY",
  } as OpenOrder;
}

describe("C3 — nested reason_code extraction through OrderActionsContext", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.restoreAllMocks());

  test("test_nested_reason_code_extracted_in_cancel", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => ({
        detail: { reason_code: "IB_CONNECTION", http_status: 503 },
      }),
    } as unknown as Response);

    const { result } = renderHook(() => useOrderActions(), { wrapper });
    let action: Awaited<ReturnType<typeof result.current.requestCancel>>;
    await act(async () => {
      action = await result.current.requestCancel(makeOrder());
    });

    expect(action!.ok).toBe(false);
    if (!action!.ok) {
      expect(action!.reasonCode).toBe("IB_CONNECTION");
      // Must equal the canonical copy, NOT generic fallback.
      expect(action!.message).toBe(ORDER_REASON_CODES.IB_CONNECTION.copy);
      expect(action!.message).not.toBe("Unknown error — see logs.");
      expect(action!.message).not.toBe("Cancel failed");
    }
    const notifs = result.current.drainNotifications();
    expect(notifs[0].message).toBe(ORDER_REASON_CODES.IB_CONNECTION.copy);
  });

  test("test_nested_reason_code_extracted_in_modify", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => ({
        detail: { reason_code: "IB_CONNECTION", http_status: 503 },
      }),
    } as unknown as Response);

    const { result } = renderHook(() => useOrderActions(), { wrapper });
    let action: Awaited<ReturnType<typeof result.current.requestModify>>;
    await act(async () => {
      action = await result.current.requestModify(makeOrder(), {
        newPrice: 501,
      });
    });

    expect(action!.ok).toBe(false);
    if (!action!.ok) {
      expect(action!.reasonCode).toBe("IB_CONNECTION");
      expect(action!.message).toBe(ORDER_REASON_CODES.IB_CONNECTION.copy);
      expect(action!.message).not.toBe("Unknown error — see logs.");
      expect(action!.message).not.toBe("Modify failed");
    }
    const notifs = result.current.drainNotifications();
    expect(notifs[0].message).toBe(ORDER_REASON_CODES.IB_CONNECTION.copy);
  });
});
