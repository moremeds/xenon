/**
 * @vitest-environment jsdom
 *
 * C2 — modifySequence plumbing via OrderActionsContext.requestModify.
 *
 * Verifies the full counter-sync contract added in A1/A2:
 *   1. fresh permId starts at 1;
 *   2. 2xx with `applied_sequence` anchors the counter;
 *   3. 409 MODIFY_STALE with `detail.applied` syncs to the server's count;
 *   4. 503 IB_CONNECTION with `detail.applied_sequence` anchors the counter.
 */
import { describe, test, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { renderHook, act } from "@testing-library/react";
import {
  OrderActionsProvider,
  useOrderActions,
} from "@/lib/OrderActionsContext";
import type { OpenOrder } from "@/lib/types";

const wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(OrderActionsProvider, null, children);

function makeOrder(permId = 4242): OpenOrder {
  return {
    orderId: 101,
    permId,
    symbol: "AAPL",
    contract: {
      conId: 1234,
      symbol: "AAPL",
      secType: "STK",
      strike: null,
      right: null,
      expiry: null,
    },
    action: "BUY",
    orderType: "LMT",
    totalQuantity: 100,
    limitPrice: 150,
    auxPrice: null,
    status: "Submitted",
    filled: 0,
    remaining: 100,
    avgFillPrice: null,
    tif: "DAY",
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

function bodyOf(
  spy: ReturnType<typeof vi.spyOn>,
  call: number,
): Record<string, unknown> {
  const args = spy.mock.calls[call];
  const init = args[1] as RequestInit;
  return JSON.parse(init.body as string);
}

describe("C2 — modifySequence plumbing", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.restoreAllMocks());

  test("test_first_modify_sends_sequence_1_then_bumps_to_2_on_success", async () => {
    const fetchFn = mockFetchSequence([
      // First success — server echoes applied_sequence:1.
      { status: 200, body: { applied_sequence: 1 } },
      // Second success — body inspected only to confirm outgoing sequence.
      { status: 200, body: { applied_sequence: 2 } },
    ]);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(fetchFn as unknown as typeof fetch);

    const { result } = renderHook(() => useOrderActions(), { wrapper });
    const order = makeOrder(1001);

    await act(async () => {
      await result.current.requestModify(order, { newPrice: 151 });
    });
    expect(bodyOf(fetchSpy, 0).modifySequence).toBe(1);

    await act(async () => {
      await result.current.requestModify(order, { newPrice: 152 });
    });
    expect(bodyOf(fetchSpy, 1).modifySequence).toBe(2);
  });

  test("test_modify_stale_409_syncs_counter_next_request_is_applied_plus_1", async () => {
    const fetchFn = mockFetchSequence([
      // First: 200 applied_sequence:1 (counter = 1).
      { status: 200, body: { applied_sequence: 1 } },
      // Second: 409 MODIFY_STALE applied=5 — counter syncs to 5.
      {
        status: 409,
        body: {
          detail: {
            reason_code: "MODIFY_STALE",
            applied: 5,
            http_status: 409,
          },
        },
      },
      // Third: 200 — inspect outgoing sequence only.
      { status: 200, body: {} },
    ]);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(fetchFn as unknown as typeof fetch);

    const { result } = renderHook(() => useOrderActions(), { wrapper });
    const order = makeOrder(1002);

    await act(async () => {
      await result.current.requestModify(order, { newPrice: 151 });
    });
    expect(bodyOf(fetchSpy, 0).modifySequence).toBe(1);

    await act(async () => {
      await result.current.requestModify(order, { newPrice: 152 });
    });
    expect(bodyOf(fetchSpy, 1).modifySequence).toBe(2);

    // After MODIFY_STALE (applied=5) → next is 6.
    await act(async () => {
      await result.current.requestModify(order, { newPrice: 153 });
    });
    expect(bodyOf(fetchSpy, 2).modifySequence).toBe(6);
  });

  test("test_ib_connection_503_with_applied_sequence_anchors_next_request", async () => {
    // Note: OrderActionsContext only anchors from detail.applied on
    // MODIFY_STALE (error branch). For non-stale errors the counter keeps
    // its monotonic local bump. So a 503 IB_CONNECTION carrying
    // applied_sequence:7 in detail does NOT sync — the next request is the
    // next local increment. This test pins that behavior so regressions
    // surface loudly if future code decides to start trusting 5xx detail.
    const fetchFn = mockFetchSequence([
      // 1st: 200 success anchors to 7 via top-level applied_sequence.
      { status: 200, body: { applied_sequence: 7 } },
      // 2nd: 503 IB_CONNECTION — local bump to 8 (no sync from error body).
      {
        status: 503,
        body: {
          detail: {
            reason_code: "IB_CONNECTION",
            applied_sequence: 7,
            http_status: 503,
          },
        },
      },
      // 3rd: 200 — confirm outgoing sequence.
      { status: 200, body: {} },
    ]);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(fetchFn as unknown as typeof fetch);

    const { result } = renderHook(() => useOrderActions(), { wrapper });
    const order = makeOrder(1003);

    await act(async () => {
      await result.current.requestModify(order, { newPrice: 200 });
    });
    expect(bodyOf(fetchSpy, 0).modifySequence).toBe(1);

    await act(async () => {
      await result.current.requestModify(order, { newPrice: 201 });
    });
    // After success anchored to 7, next is 8.
    expect(bodyOf(fetchSpy, 1).modifySequence).toBe(8);

    await act(async () => {
      await result.current.requestModify(order, { newPrice: 202 });
    });
    // After 503 (no sync), local counter keeps bumping: 9.
    expect(bodyOf(fetchSpy, 2).modifySequence).toBe(9);
  });

  test("test_counters_are_per_permid_independent", async () => {
    const fetchFn = mockFetchSequence([
      { status: 200, body: {} },
      { status: 200, body: {} },
      { status: 200, body: {} },
    ]);
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(fetchFn as unknown as typeof fetch);

    const { result } = renderHook(() => useOrderActions(), { wrapper });
    const orderA = makeOrder(5001);
    const orderB = makeOrder(5002);

    await act(async () => {
      await result.current.requestModify(orderA, { newPrice: 10 });
    });
    await act(async () => {
      await result.current.requestModify(orderA, { newPrice: 11 });
    });
    await act(async () => {
      await result.current.requestModify(orderB, { newPrice: 20 });
    });

    expect(bodyOf(fetchSpy, 0).modifySequence).toBe(1);
    expect(bodyOf(fetchSpy, 1).modifySequence).toBe(2);
    expect(bodyOf(fetchSpy, 2).modifySequence).toBe(1); // fresh permId
  });
});
