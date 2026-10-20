// @vitest-environment jsdom
/**
 * PR-10 (single order owner) — the three owner reason codes reach the user.
 *
 * FastAPI answers ORDER_NOT_OWNED (409), IB_BROKER_DISCONNECTED (503) and
 * ORDERS_HALTED (503) with a top-level reason_code. The Next routes pass
 * status + body through unchanged, the toast shows the mapped copy, and a
 * place refused with one of them is TERMINAL (nothing was sent): it never
 * becomes PLACE_OUTCOME_UNKNOWN. PLACE_OUTCOME_UNKNOWN itself is unchanged.
 */
import React from "react";
import { describe, test, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

vi.mock("@/lib/xenonApi", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/xenonApi")>("@/lib/xenonApi");
  return { ...actual, xenonFetch: vi.fn() };
});

import { xenonFetch, XenonApiError } from "@/lib/xenonApi";
import { POST as placePost } from "../app/api/orders/place/route";
import { POST as cancelPost } from "../app/api/orders/cancel/route";
import { POST as modifyPost } from "../app/api/orders/modify/route";
import { ORDER_REASON_CODES } from "@/lib/orderReasonCodes";
import { placeOutcomeUnknownBody } from "@/lib/order/placeOutcome";
import { submitPlaceOrder, type PlaceAttempt } from "@/lib/order/submitPlace";
import {
  OrderActionsProvider,
  useOrderActions,
} from "@/lib/OrderActionsContext";
import type { OpenOrder } from "@/lib/types";

const mockFetch = xenonFetch as unknown as ReturnType<typeof vi.fn>;

const OWNER_CODES = [
  [409, "ORDER_NOT_OWNED"],
  [503, "IB_BROKER_DISCONNECTED"],
  [503, "ORDERS_HALTED"],
] as const;

// What FastAPI's order_error() sends: top-level reason_code + detail.
function ownerBody(code: string, status: number) {
  return {
    reason_code: code,
    detail: {
      reason_code: code,
      message: `${code} from the order owner`,
      http_status: status,
    },
  };
}

function req(path: string, body: Record<string, unknown>): Request {
  return new Request(`http://localhost/api/orders/${path}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

const PLACE = {
  type: "stock",
  symbol: "SPY",
  action: "BUY",
  quantity: 1,
  limitPrice: 500,
  client_attempt_id: "owner-codes-1",
};

describe("PR-10 owner reason codes — Next route passthrough", () => {
  beforeEach(() => mockFetch.mockReset());

  test.each(OWNER_CODES)(
    "cancel and modify keep %i %s verbatim",
    async (status, code) => {
      for (const [post, body] of [
        [cancelPost, { orderId: 123, permId: 456 }],
        [modifyPost, { orderId: 123, permId: 456, newPrice: 5.5 }],
      ] as const) {
        mockFetch.mockRejectedValueOnce(
          new XenonApiError(status, code, ownerBody(code, status)),
        );
        const res = await post(
          req(post === cancelPost ? "cancel" : "modify", body),
        );
        expect(res.status).toBe(status);
        expect((await res.json()).reason_code).toBe(code);
      }
    },
  );

  test.each([
    [503, "IB_BROKER_DISCONNECTED"],
    [503, "ORDERS_HALTED"],
    [503, "IB_CONNECTION"],
  ])(
    "place refused before the send (%i %s) stays terminal",
    async (status, code) => {
      mockFetch.mockRejectedValueOnce(
        new XenonApiError(status, code, {
          detail: "nothing was sent",
          reason_code: code,
        }),
      );
      const res = await placePost(req("place", PLACE));
      expect(res.status).toBe(status);
      const body = await res.json();
      expect(body.reason_code).toBe(code);
      expect(placeOutcomeUnknownBody({ status, body })).toBeNull();
    },
  );

  test("place 502 PLACE_OUTCOME_UNKNOWN is unchanged", async () => {
    const unknown = {
      detail:
        "Order outcome unknown — check working orders before placing it again. Do not resubmit.",
      reason_code: "PLACE_OUTCOME_UNKNOWN",
      submission_id: "sub-1",
    };
    mockFetch.mockRejectedValueOnce(new XenonApiError(502, "unknown", unknown));
    const res = await placePost(req("place", PLACE));
    expect(res.status).toBe(502);
    expect(await res.json()).toMatchObject(unknown);
  });
});

function attempt(): PlaceAttempt & { state: string } {
  const a = {
    id: "owner-codes-attempt",
    state: "new",
    isUncertain: () => a.state === "uncertain",
    markSubmitted: () => void (a.state = "submitted"),
    markTerminal: () => void (a.state = "terminal"),
    markUncertain: () => void (a.state = "uncertain"),
  };
  return a;
}

function fetchReturns(status: number, body: unknown) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
    ok: status < 400,
    status,
    json: async () => body,
  } as unknown as Response);
}

describe("PR-10 owner reason codes — browser-side toasts", () => {
  afterEach(() => vi.restoreAllMocks());

  test.each(OWNER_CODES)(
    "place %i %s: terminal toast with the mapped copy",
    async (status, code) => {
      fetchReturns(status, { detail: "x", reason_code: code });
      const a = attempt();
      const outcome = await submitPlaceOrder(a, PLACE);
      expect(outcome).toEqual({
        kind: "terminal",
        message: ORDER_REASON_CODES[code].copy,
      });
      expect(a.state).toBe("terminal");
    },
  );

  test("place 502 PLACE_OUTCOME_UNKNOWN keeps the attempt uncertain", async () => {
    fetchReturns(502, {
      detail: "x",
      reason_code: "PLACE_OUTCOME_UNKNOWN",
      submission_id: "s",
    });
    const a = attempt();
    const outcome = await submitPlaceOrder(a, PLACE);
    expect(outcome).toEqual({
      kind: "uncertain",
      message: ORDER_REASON_CODES.PLACE_OUTCOME_UNKNOWN.copy,
    });
    expect(a.state).toBe("uncertain");
  });

  const order = {
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

  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(OrderActionsProvider, null, children);

  test.each(OWNER_CODES)(
    "cancel and modify %i %s toast the mapped copy",
    async (status, code) => {
      const { result } = renderHook(() => useOrderActions(), { wrapper });
      for (const run of [
        () => result.current.requestCancel(order),
        () => result.current.requestModify(order, { newPrice: 501 }),
      ]) {
        fetchReturns(status, ownerBody(code, status));
        let action: Awaited<ReturnType<typeof run>> | undefined;
        await act(async () => {
          action = await run();
        });
        expect(action!.ok).toBe(false);
        if (!action!.ok) {
          expect(action!.reasonCode).toBe(code);
          expect(action!.message).toBe(ORDER_REASON_CODES[code].copy);
        }
        expect(result.current.drainNotifications()[0].message).toBe(
          ORDER_REASON_CODES[code].copy,
        );
      }
    },
  );
});
