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

  test("test_cancel_top_level_reason_code_renders_toast_copy", async () => {
    // PR-05 shape: FastAPI now emits reason_code at the TOP level (the toast
    // helper reads body.reason_code first); detail keeps the structured
    // payload for nested readers.
    const fetchFn = mockFetchSequence([
      {
        status: 503,
        body: {
          reason_code: "IB_CONNECTION",
          detail: {
            reason_code: "IB_CONNECTION",
            classification: "connection",
            message: "IB gateway unreachable",
            http_status: 503,
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
      expect(r!.status).toBe(503);
      expect(r!.reasonCode).toBe("IB_CONNECTION");
      expect(r!.message).toBe(ORDER_REASON_CODES.IB_CONNECTION.copy);
    }

    const notifs = result.current.drainNotifications();
    expect(notifs).toHaveLength(1);
    expect(notifs[0].message).toBe(ORDER_REASON_CODES.IB_CONNECTION.copy);
  });

  test("test_cancel_502_string_detail_shows_upstream_detail_in_toast", async () => {
    // Lead browser repro: FastAPI 502 {detail: "..."} passes through the
    // Next route verbatim with no reason_code — the toast must carry the
    // human-readable upstream detail, not the generic "Cancel failed".
    const detail = "Cancel not confirmed by refreshed IB open orders";
    const fetchFn = mockFetchSequence([{ status: 502, body: { detail } }]);
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
      expect(r!.status).toBe(502);
      expect(r!.reasonCode).toBeNull();
      expect(r!.message).toBe(detail);
    }
    const notifs = result.current.drainNotifications();
    expect(notifs).toHaveLength(1);
    expect(notifs[0].message).toBe(detail);
  });

  test("test_cancel_object_detail_message_shows_upstream_message", async () => {
    // FastAPI HTTPException({detail: {message: ...}}) without reason_code —
    // the nested message is still human-readable and must surface.
    const fetchFn = mockFetchSequence([
      { status: 502, body: { detail: { message: "IB rejected: order filled" } } },
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
      expect(r!.reasonCode).toBeNull();
      expect(r!.message).toBe("IB rejected: order filled");
    }
    const notifs = result.current.drainNotifications();
    expect(notifs[0].message).toBe("IB rejected: order filled");
  });

  test("test_modify_502_string_detail_shows_upstream_detail_in_toast", async () => {
    const detail = "Modify not confirmed by refreshed IB open orders";
    const fetchFn = mockFetchSequence([{ status: 502, body: { detail } }]);
    vi.spyOn(globalThis, "fetch").mockImplementation(
      fetchFn as unknown as typeof fetch,
    );

    const { result } = renderHook(() => useOrderActions(), { wrapper });

    let r: Awaited<ReturnType<typeof result.current.requestModify>>;
    await act(async () => {
      r = await result.current.requestModify(makeOrder(), { newPrice: 5.5 });
    });

    expect(r!.ok).toBe(false);
    if (!r!.ok) {
      expect(r!.status).toBe(502);
      expect(r!.reasonCode).toBeNull();
      expect(r!.message).toBe(detail);
    }
    const notifs = result.current.drainNotifications();
    expect(notifs[0].message).toBe(detail);
  });

  test("test_modify_object_detail_message_shows_upstream_message", async () => {
    const fetchFn = mockFetchSequence([
      {
        status: 502,
        body: { detail: { message: "IB rejected modify: duplicate id" } },
      },
    ]);
    vi.spyOn(globalThis, "fetch").mockImplementation(
      fetchFn as unknown as typeof fetch,
    );

    const { result } = renderHook(() => useOrderActions(), { wrapper });

    let r: Awaited<ReturnType<typeof result.current.requestModify>>;
    await act(async () => {
      r = await result.current.requestModify(makeOrder(), { newPrice: 5.5 });
    });

    expect(r!.ok).toBe(false);
    if (!r!.ok) {
      expect(r!.message).toBe("IB rejected modify: duplicate id");
    }
    const notifs = result.current.drainNotifications();
    expect(notifs[0].message).toBe("IB rejected modify: duplicate id");
  });

  test("test_cancel_non_json_upstream_detail_reaches_toast", async () => {
    // Non-JSON upstream (e.g. gateway 502 text) — the route emits
    // {detail: <raw text>} which the toast must show verbatim.
    const fetchFn = mockFetchSequence([
      { status: 502, body: { detail: "upstream unavailable" } },
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
      expect(r!.message).toBe("upstream unavailable");
    }
    const notifs = result.current.drainNotifications();
    expect(notifs[0].message).toBe("upstream unavailable");
  });

  test("test_modify_non_json_upstream_detail_reaches_toast", async () => {
    const fetchFn = mockFetchSequence([
      { status: 502, body: { detail: "upstream unavailable" } },
    ]);
    vi.spyOn(globalThis, "fetch").mockImplementation(
      fetchFn as unknown as typeof fetch,
    );

    const { result } = renderHook(() => useOrderActions(), { wrapper });

    let r: Awaited<ReturnType<typeof result.current.requestModify>>;
    await act(async () => {
      r = await result.current.requestModify(makeOrder(), { newPrice: 5.5 });
    });

    expect(r!.ok).toBe(false);
    if (!r!.ok) {
      expect(r!.message).toBe("upstream unavailable");
    }
    const notifs = result.current.drainNotifications();
    expect(notifs[0].message).toBe("upstream unavailable");
  });

  test("test_reason_code_copy_still_wins_over_detail_message", async () => {
    // When reason_code IS present its curated copy still wins — the detail
    // fallback only applies when no reason_code exists.
    const fetchFn = mockFetchSequence([
      {
        status: 404,
        body: {
          reason_code: "IB_REJECT",
          detail: "Order not found (local lookup)",
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
    expect(notifs[0].message).toBe(ORDER_REASON_CODES.IB_REJECT.copy);
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
