/**
 * @vitest-environment jsdom
 *
 * C1 (downgraded from Playwright) — cancel/modify failure rendering
 *
 * The repo has a Playwright suite under web/e2e/, but the three scenarios in
 * the task brief are really about OrderActionsContext's contract:
 *   - requestCancel/requestModify return {ok:false,...} so the caller keeps
 *     the modal open (WorkspaceSections checks `result.ok` before closing);
 *   - the toast copy pulled from readReasonCode → getReasonToast;
 *   - modifySequence auto-bumps after a MODIFY_STALE response (A1).
 *
 * A real browser test would re-verify the two upstream layers (Next.js route
 * passthrough + FastAPI reason_code emission), which C3 and F6.3 already
 * cover. Exercising OrderActionsContext via renderHook gives faster, hermetic
 * coverage of the actual failure-rendering code path. Downgrade noted.
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

function makeOrder(overrides: Partial<OpenOrder> = {}): OpenOrder {
  return {
    orderId: 101,
    permId: 9001,
    symbol: "TSLL",
    contract: {
      conId: 5001,
      symbol: "TSLL",
      secType: "STK",
      strike: null,
      right: null,
      expiry: null,
    },
    action: "SELL",
    orderType: "LMT",
    totalQuantity: 500,
    limitPrice: 12.34,
    auxPrice: null,
    status: "Submitted",
    filled: 0,
    remaining: 500,
    avgFillPrice: null,
    tif: "GTC",
    ...overrides,
  } as OpenOrder;
}

function mockFetchSequence(
  responses: Array<{ status: number; body: unknown }>,
): ReturnType<typeof vi.fn> {
  const fn = vi.fn();
  for (const r of responses) {
    fn.mockResolvedValueOnce({
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      json: async () => r.body,
    });
  }
  return fn;
}

describe("C1 — cancel/modify failure rendering (Vitest downgrade)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  test("test_cancel_with_gateway_down_returns_failure_and_preserves_open_modal_contract", async () => {
    // 503 with nested reason_code — exactly what FastAPI returns and
    // /api/orders/cancel preserves verbatim.
    const fetchFn = mockFetchSequence([
      {
        status: 503,
        body: {
          detail: {
            reason_code: "IB_CONNECTION",
            message: "IB connection lost",
            http_status: 503,
          },
        },
      },
    ]);
    vi.spyOn(globalThis, "fetch").mockImplementation(
      fetchFn as unknown as typeof fetch,
    );

    const { result } = renderHook(() => useOrderActions(), { wrapper });

    let actionResult: Awaited<ReturnType<typeof result.current.requestCancel>>;
    await act(async () => {
      actionResult = await result.current.requestCancel(makeOrder());
    });

    // (b) "modal stays open" contract: requestCancel must return ok:false so
    // WorkspaceSections doesn't call setCancelTarget(null).
    expect(actionResult!.ok).toBe(false);
    if (!actionResult!.ok) {
      expect(actionResult!.status).toBe(503);
      expect(actionResult!.reasonCode).toBe("IB_CONNECTION");
      // (a) toast contains exact IB_CONNECTION copy.
      expect(actionResult!.message).toBe(ORDER_REASON_CODES.IB_CONNECTION.copy);
    }

    // Toast was pushed with the reason copy.
    const notifs = result.current.drainNotifications();
    expect(notifs).toHaveLength(1);
    expect(notifs[0].type).toBe("error");
    expect(notifs[0].message).toBe(ORDER_REASON_CODES.IB_CONNECTION.copy);

    // (c)/(d) pendingCancels not populated → no optimistic cancel, caller can
    // render a FAILED pill and re-enable the Cancel button.
    expect(result.current.pendingCancels.size).toBe(0);
  });

  test("test_modify_stale_keeps_modal_open_and_autobumps_sequence_on_retry", async () => {
    const order = makeOrder({ permId: 7777 });
    const fetchFn = mockFetchSequence([
      // First attempt: 409 MODIFY_STALE, server says applied=3
      {
        status: 409,
        body: {
          detail: {
            reason_code: "MODIFY_STALE",
            applied: 3,
            http_status: 409,
          },
        },
      },
      // Second attempt: 200 — body without orders so no background poll fires.
      { status: 200, body: {} },
    ]);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(fetchFn as unknown as typeof fetch);

    const { result } = renderHook(() => useOrderActions(), { wrapper });

    let first: Awaited<ReturnType<typeof result.current.requestModify>>;
    await act(async () => {
      first = await result.current.requestModify(order, { newPrice: 5.5 });
    });

    expect(first!.ok).toBe(false);
    if (!first!.ok) {
      expect(first!.reasonCode).toBe("MODIFY_STALE");
      expect(first!.message).toBe(ORDER_REASON_CODES.MODIFY_STALE.copy);
      expect(first!.status).toBe(409);
    }

    // Inspect first body: modifySequence should be 1.
    const firstCallBody = JSON.parse(
      (fetchSpy.mock.calls[0][1] as RequestInit).body as string,
    );
    expect(firstCallBody.modifySequence).toBe(1);

    // Retry — A1 sync sets counter to applied (3); next attempt should send 4.
    await act(async () => {
      await result.current.requestModify(order, { newPrice: 5.6 });
    });
    const secondCallBody = JSON.parse(
      (fetchSpy.mock.calls[1][1] as RequestInit).body as string,
    );
    expect(secondCallBody.modifySequence).toBe(4);
  });

  test("test_cancel_unknown_order_upstream_10147_renders_ib_reject_copy", async () => {
    const fetchFn = mockFetchSequence([
      {
        status: 404,
        body: {
          detail: {
            reason_code: "IB_REJECT",
            upstream: { code: 10147, message: "Order not found" },
            http_status: 404,
          },
        },
      },
    ]);
    vi.spyOn(globalThis, "fetch").mockImplementation(
      fetchFn as unknown as typeof fetch,
    );

    const { result } = renderHook(() => useOrderActions(), { wrapper });

    let r: Awaited<ReturnType<typeof result.current.requestCancel>>;
    await act(async () => {
      r = await result.current.requestCancel(makeOrder());
    });

    expect(r!.ok).toBe(false);
    if (!r!.ok) {
      expect(r!.reasonCode).toBe("IB_REJECT");
      expect(r!.message).toBe(ORDER_REASON_CODES.IB_REJECT.copy);
    }

    const notifs = result.current.drainNotifications();
    expect(notifs).toHaveLength(1);
    expect(notifs[0].message).toBe(ORDER_REASON_CODES.IB_REJECT.copy);
  });
});
