// @vitest-environment jsdom
/**
 * PR-07 (OP-1): an uncertain place (FastAPI 502 PLACE_OUTCOME_UNKNOWN, or a
 * Next-side timeout) is never shown as a plain failure. Every consumer keeps
 * the body; the UI says "submitted, status unknown — do not resubmit", keeps
 * the client_attempt_id and offers no retry.
 */
import React from "react";
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, cleanup, fireEvent, waitFor, renderHook, act } from "@testing-library/react";
import type { PortfolioPosition } from "@/lib/types";

const xenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", async () => {
  const actual = await vi.importActual<typeof import("@/lib/xenonApi")>("@/lib/xenonApi");
  return { ...actual, xenonFetch: (...args: unknown[]) => xenonFetch(...args) };
});
vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div data-testid="modal">{children}</div>,
}));

import { XenonApiError } from "@/lib/xenonApi";
import { isPlaceOutcomeUnknown, errorFromResponseBody } from "@/lib/order/responseError";
import { useClientAttemptId } from "@/components/ticker-detail/useClientAttemptId";
import PositionOrderModal from "@/components/PositionOrderModal";

const UNKNOWN_BODY = {
  detail: "Order outcome unknown — check working orders before placing it again. Do not resubmit.",
  reason_code: "PLACE_OUTCOME_UNKNOWN",
  submission_id: "sub-1",
};
const placeBody = {
  type: "stock",
  symbol: "SPY",
  action: "BUY",
  quantity: 1,
  limitPrice: 500,
  client_attempt_id: "c-unc-1",
};

function post(url: string, body: unknown) {
  return new Request(url, { method: "POST", body: JSON.stringify(body) });
}

beforeEach(() => xenonFetch.mockReset());
afterEach(() => cleanup());

describe("Next /api/orders/place — uncertain outcome", () => {
  it("passes FastAPI's 502 PLACE_OUTCOME_UNKNOWN body through unchanged", async () => {
    xenonFetch.mockRejectedValueOnce(new XenonApiError(502, UNKNOWN_BODY.detail, UNKNOWN_BODY));
    const { POST } = await import("../app/api/orders/place/route");
    const res = await POST(post("http://localhost/api/orders/place", placeBody));
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual(UNKNOWN_BODY);
  });

  it("maps its own fetch timeout to 502 PLACE_OUTCOME_UNKNOWN, not a 500", async () => {
    xenonFetch.mockRejectedValueOnce(Object.assign(new Error("timed out"), { name: "TimeoutError" }));
    const { POST } = await import("../app/api/orders/place/route");
    const res = await POST(post("http://localhost/api/orders/place", placeBody));
    expect(res.status).toBe(502);
    expect((await res.json()).reason_code).toBe("PLACE_OUTCOME_UNKNOWN");
  });

  it("keeps persist_warning on a 200 accepted-but-not-saved place", async () => {
    xenonFetch
      .mockResolvedValueOnce({ status: "ok", orderId: 7, permId: 8, persist_warning: "not saved", submission_id: "s" })
      .mockResolvedValueOnce({});
    const { POST } = await import("../app/api/orders/place/route");
    const res = await POST(post("http://localhost/api/orders/place", placeBody));
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.persist_warning).toBe("not saved");
    expect(body.orderId).toBe(7);
  });
});

describe("Next /api/orders/modify — combo replacement after cancel", () => {
  const replaceBody = {
    orderId: 7001,
    replaceOrder: {
      type: "combo",
      symbol: "AAPL",
      action: "BUY",
      quantity: 1,
      limitPrice: 0.75,
      client_attempt_id: "c-rep-1",
      legs: [
        { expiry: "20260327", strike: 190, right: "C", action: "BUY", ratio: 1 },
        { expiry: "20260327", strike: 200, right: "C", action: "SELL", ratio: 1 },
      ],
    },
  };

  it("an uncertain replacement keeps PLACE_OUTCOME_UNKNOWN, never 'place a new order manually'", async () => {
    xenonFetch.mockImplementation(async (path: string) => {
      if (path === "/orders/cancel") return { status: "ok" };
      if (path === "/orders/place") throw new XenonApiError(502, UNKNOWN_BODY.detail, UNKNOWN_BODY);
      return { orders: [] };
    });
    const { POST } = await import("../app/api/orders/modify/route");
    const res = await POST(post("http://localhost/api/orders/modify", replaceBody));
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.reason_code).toBe("PLACE_OUTCOME_UNKNOWN");
    expect(body.error).not.toMatch(/Place a new order manually/);
    expect(xenonFetch.mock.calls.filter(([p]) => p === "/orders/place")).toHaveLength(1);
  });

  it("an uncertain cancel throws before any replacement is placed", async () => {
    xenonFetch.mockImplementation(async (path: string) => {
      if (path === "/orders/cancel") throw new XenonApiError(503, "gateway", { reason_code: "IB_CONNECTION" });
      return { orders: [] };
    });
    const { POST } = await import("../app/api/orders/modify/route");
    const res = await POST(post("http://localhost/api/orders/modify", replaceBody));
    expect(res.status).toBe(503);
    expect(xenonFetch.mock.calls.some(([p]) => p === "/orders/place")).toBe(false);
  });
});

describe("UI — submitted, status unknown", () => {
  it("copy says submitted / do not resubmit", () => {
    expect(isPlaceOutcomeUnknown(UNKNOWN_BODY)).toBe(true);
    expect(isPlaceOutcomeUnknown({ reason_code: "IB_REJECT" })).toBe(false);
    expect(errorFromResponseBody(UNKNOWN_BODY, "x")).toMatch(/submitted, status unknown.*Do not resubmit/i);
  });

  it("markUncertain keeps the attempt id; editing the order mints a new one", () => {
    const { result } = renderHook(() => useClientAttemptId({ ticker: "SPY" }));
    const first = result.current.id;
    act(() => result.current.markSubmitted());
    act(() => result.current.markUncertain());
    expect(result.current.id).toBe(first);
    expect(result.current.uncertain).toBe(true);
    act(() => result.current.onFieldEdit("qty"));
    expect(result.current.id).not.toBe(first);
    expect(result.current.uncertain).toBe(false);
  });

  it("PositionOrderModal shows the message and disables submit (no retry)", async () => {
    const pos = {
      id: 1, ticker: "TSLA", structure: "Stock", structure_type: "Stock", risk_profile: "equity",
      expiry: "", contracts: 300, direction: "LONG", entry_cost: 96000, max_risk: null, market_value: 105000,
      legs: [{ direction: "LONG", contracts: 300, type: "Stock", strike: null, entry_cost: 96000, avg_cost: 320,
        market_price: 350, market_value: 105000, market_price_is_calculated: false }],
      ib_daily_pnl: null, kelly_optimal: null, target: null, stop: null, entry_date: "",
    } as unknown as PortfolioPosition;
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 502, json: async () => UNKNOWN_BODY });
    (global as any).fetch = fetchMock;
    const { getByRole, findByText } = render(
      <PositionOrderModal position={pos} prices={{ TSLA: { last: 350, bid: 349.9, ask: 350.1 } as any }} onClose={() => {}} />,
    );
    const submit = getByRole("button", { name: /^Submit/i });
    fireEvent.click(submit);
    await findByText(/submitted, status unknown/i);
    await waitFor(() => expect((getByRole("button", { name: /^Submit/i }) as HTMLButtonElement).disabled).toBe(true));
    fireEvent.click(getByRole("button", { name: /^Submit/i }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
