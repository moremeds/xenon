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
vi.mock("@/lib/OrderActionsContext", () => ({
  useOrderActions: () => ({
    pendingCancels: new Map(),
    pendingModifies: new Map(),
    cancelledOrders: [],
    requestCancel: vi.fn(),
    requestModify: vi.fn(),
    drainNotifications: vi.fn(() => []),
    setOrdersUpdater: vi.fn(),
  }),
}));
vi.mock("@/components/ModifyOrderModal", () => ({ default: () => null }));
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

// Block body: a function returned from beforeEach runs as a cleanup hook.
beforeEach(() => {
  xenonFetch.mockReset();
});
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

  it.each([
    ["socket reset (TypeError)", new TypeError("fetch failed")],
    ["5xx without a reason_code", new XenonApiError(500, "Internal Server Error", null)],
    ["unreadable 200 body", new SyntaxError("Unexpected token")],
  ])("after dispatch, %s → 502 PLACE_OUTCOME_UNKNOWN", async (_n, err) => {
    xenonFetch.mockRejectedValueOnce(err);
    const { POST } = await import("../app/api/orders/place/route");
    const res = await POST(post("http://localhost/api/orders/place", placeBody));
    expect(res.status).toBe(502);
    expect((await res.json()).reason_code).toBe("PLACE_OUTCOME_UNKNOWN");
  });

  it("a proven rejection (4xx, or 5xx with a known reason_code) stays terminal", async () => {
    const { POST } = await import("../app/api/orders/place/route");
    xenonFetch.mockRejectedValueOnce(new XenonApiError(502, "rejected", { reason_code: "IB_REJECT", detail: "rejected" }));
    expect((await (await POST(post("http://localhost/api/orders/place", placeBody))).json()).reason_code).toBe("IB_REJECT");
    xenonFetch.mockRejectedValueOnce(new XenonApiError(409, "terminal", { reason_code: "ATTEMPT_ID_TERMINAL" }));
    const r409 = await POST(post("http://localhost/api/orders/place", placeBody));
    expect(r409.status).toBe(409);
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

  it("an uncertain replacement keeps the FastAPI PLACE_OUTCOME_UNKNOWN body, never 'place a new order manually'", async () => {
    xenonFetch.mockImplementation(async (path: string) => {
      if (path === "/orders/replace") throw new XenonApiError(502, UNKNOWN_BODY.detail, UNKNOWN_BODY);
      throw new Error(`unexpected ${path}`);
    });
    const { POST } = await import("../app/api/orders/modify/route");
    const res = await POST(post("http://localhost/api/orders/modify", replaceBody));
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body).toEqual(UNKNOWN_BODY);
    expect(xenonFetch).toHaveBeenCalledTimes(1);
  });

  it("an invalid replacement (no client_attempt_id) cancels nothing", async () => {
    xenonFetch.mockResolvedValue({ status: "ok" });
    const { POST } = await import("../app/api/orders/modify/route");
    const { client_attempt_id: _drop, ...noId } = replaceBody.replaceOrder;
    const res = await POST(post("http://localhost/api/orders/modify", { ...replaceBody, replaceOrder: noId }));
    expect(res.status).toBe(400);
    expect((await res.json()).reason_code).toBe("INVALID_ORDER_BODY");
    expect(xenonFetch).not.toHaveBeenCalled();
  });

  it.each([
    ["socket reset (TypeError)", new TypeError("fetch failed")],
    ["5xx without a reason_code", new XenonApiError(500, "Internal Server Error", null)],
  ])("replacement forward %s is uncertain, never 'place a new order manually'", async (_n, err) => {
    xenonFetch.mockImplementation(async () => {
      throw err;
    });
    const { POST } = await import("../app/api/orders/modify/route");
    const res = await POST(post("http://localhost/api/orders/modify", replaceBody));
    const body = await res.json();
    expect(res.status).toBe(502);
    expect(body.reason_code).toBe("PLACE_OUTCOME_UNKNOWN");
    expect(String(body.error ?? "")).not.toMatch(/Place a new order manually/);
  });
});

describe("UI — submitted, status unknown", () => {
  it("copy says submitted / do not resubmit", () => {
    expect(isPlaceOutcomeUnknown(UNKNOWN_BODY)).toBe(true);
    expect(isPlaceOutcomeUnknown({ reason_code: "IB_REJECT" })).toBe(false);
    expect(errorFromResponseBody(UNKNOWN_BODY, "x")).toMatch(/submitted, status unknown.*Do not resubmit/i);
  });

  it("uncertain is sticky: no later response or field edit rotates the id", () => {
    const { result } = renderHook(() => useClientAttemptId({ ticker: "SPY" }));
    const first = result.current.id;
    act(() => result.current.markSubmitted());
    act(() => result.current.markUncertain());
    act(() => result.current.markTerminal());
    act(() => result.current.onFieldEdit("qty"));
    expect(result.current.id).toBe(first);
    expect(result.current.uncertain).toBe(true);
    expect(result.current.isUncertain()).toBe(true);
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

describe("submitPlaceOrder — every form's single place entry", () => {
  type Reply = () => Promise<unknown>;
  const ok = (status: number, body: unknown) => async () => ({
    ok: status < 400,
    status,
    json: async () => body,
  });
  const cases: Array<[string, Reply, "ok" | "uncertain" | "terminal"]> = [
    ["502 PLACE_OUTCOME_UNKNOWN", ok(502, UNKNOWN_BODY), "uncertain"],
    ["network failure after dispatch (TypeError)", async () => { throw new TypeError("fetch failed"); }, "uncertain"],
    ["500 without a reason_code", ok(500, { error: "internal" }), "uncertain"],
    ["200 with an unreadable body", async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError("x"); } }), "uncertain"],
    ["502 IB_REJECT", ok(502, { reason_code: "IB_REJECT", detail: "rejected" }), "terminal"],
    ["400 validation", ok(400, { reason_code: "INVALID_ORDER_BODY" }), "terminal"],
    ["200 accepted", ok(200, { status: "ok", orderId: 1 }), "ok"],
  ];

  it.each(cases)("%s", async (_name, reply, kind) => {
    const { submitPlaceOrder } = await import("@/lib/order/submitPlace");
    const fetchMock = vi.fn(reply);
    (global as any).fetch = fetchMock;
    const { result } = renderHook(() => useClientAttemptId({ ticker: "SPY" }));
    const first = result.current.id;
    let out: Awaited<ReturnType<typeof submitPlaceOrder>> | undefined;
    await act(async () => {
      out = await submitPlaceOrder(result.current, { symbol: "SPY" });
    });
    expect(out!.kind).toBe(kind);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).client_attempt_id).toBe(first);
    if (kind === "uncertain") {
      expect(result.current.id).toBe(first);
      expect(result.current.uncertain).toBe(true);
      // A second submit from the same form is refused before any fetch.
      let again: typeof out;
      await act(async () => {
        again = await submitPlaceOrder(result.current, { symbol: "SPY" });
      });
      expect(again!.kind).toBe("uncertain");
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } else {
      expect(result.current.id).not.toBe(first);
    }
  });
});

describe("BookTab stock form — uncertain place blocks a resubmit", () => {
  it("shows the message, disables submit, never sends twice", async () => {
    const BookTab = (await import("@/components/ticker-detail/BookTab")).default;
    const fetchMock = vi.fn(async () => ({ ok: false, status: 502, json: async () => UNKNOWN_BODY }));
    (global as any).fetch = fetchMock;
    const { getByPlaceholderText, getByRole, findByText } = render(
      <BookTab ticker="SPY" position={null} prices={{}} openOrders={[]} tickerPriceData={null} />,
    );
    fireEvent.change(getByPlaceholderText("Shares"), { target: { value: "1" } });
    fireEvent.change(getByPlaceholderText("0.00"), { target: { value: "500" } });
    fireEvent.click(getByRole("button", { name: /^Place Order$/ }));
    fireEvent.click(getByRole("button", { name: /^Confirm Order$/ }));
    await findByText(/submitted, status unknown/i);
    const confirm = getByRole("button", { name: /Confirm Order|Placing/ }) as HTMLButtonElement;
    await waitFor(() => expect(confirm.disabled).toBe(true));
    fireEvent.click(confirm);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

async function expectBlockedAfterUncertain(
  ui: React.ReactElement,
  qtyPlaceholder: string,
) {
  const fetchMock = vi.fn(async () => ({ ok: false, status: 502, json: async () => UNKNOWN_BODY }));
  (global as any).fetch = fetchMock;
  const { getByPlaceholderText, getByRole, findByText } = render(ui);
  fireEvent.change(getByPlaceholderText(qtyPlaceholder), { target: { value: "1" } });
  fireEvent.change(getByPlaceholderText("0.00"), { target: { value: "5" } });
  fireEvent.click(getByRole("button", { name: /^Place Order$/ }));
  fireEvent.click(getByRole("button", { name: /^Confirm Order$/ }));
  await findByText(/submitted, status unknown/i);
  const confirm = getByRole("button", { name: /Confirm Order|Placing/ }) as HTMLButtonElement;
  await waitFor(() => expect(confirm.disabled).toBe(true));
  fireEvent.click(confirm);
  expect(fetchMock.mock.calls.filter(([u]) => u === "/api/orders/place")).toHaveLength(1);
}

describe("OrderTab / InstrumentDetailModal — uncertain place blocks a resubmit", () => {
  it("OrderTab new-order form", async () => {
    const OrderTab = (await import("@/components/ticker-detail/OrderTab")).default;
    const { TickerDetailProvider } = await import("@/lib/TickerDetailContext");
    await expectBlockedAfterUncertain(
      <TickerDetailProvider>
        <OrderTab ticker="QQQ" position={null} portfolio={null} prices={{}} openOrders={[]} tickerPriceData={null} />
      </TickerDetailProvider>,
      "Shares",
    );
  });

  it("InstrumentDetailModal option form", async () => {
    const InstrumentDetailModal = (await import("@/components/InstrumentDetailModal")).default;
    const leg = {
      direction: "LONG", contracts: 1, type: "Call", strike: 500, entry_cost: 100, avg_cost: 1,
      market_price: 1, market_value: 100,
    } as any;
    await expectBlockedAfterUncertain(
      <InstrumentDetailModal leg={leg} ticker="SPY" expiry="2026-12-18" prices={{}} onClose={() => {}} />,
      "Contracts",
    );
  });
});
