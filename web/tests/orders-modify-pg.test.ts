import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/server", () => ({
  NextResponse: {
    json: (body: unknown, init?: ResponseInit) =>
      new Response(JSON.stringify(body), {
        ...init,
        headers: {
          "content-type": "application/json",
          ...(init?.headers ?? {}),
        },
      }),
  },
}));

const mocks = vi.hoisted(() => ({
  xenonFetch: vi.fn(),
}));

vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: mocks.xenonFetch,
  XenonApiError: class XenonApiError extends Error {
    status: number;
    detail: unknown;
    body?: unknown;

    constructor(status: number, detail: unknown, body?: unknown) {
      super(`Xenon API ${status}: ${String(detail)}`);
      this.name = "XenonApiError";
      this.status = status;
      this.detail = detail;
      this.body = body;
    }
  },
}));

const confirmedOrders = {
  last_sync: "2026-03-10T15:00:00Z",
  open_orders: [
    {
      orderId: 7001,
      permId: 9001,
      symbol: "AAPL",
      totalQuantity: 25,
      limitPrice: 5.5,
    },
  ],
  executed_orders: [],
  open_count: 1,
  executed_count: 0,
};

const replacementOrders = {
  last_sync: "2026-03-10T15:01:00Z",
  open_orders: [{ orderId: 7002, permId: 9002, symbol: "AAPL" }],
  executed_orders: [],
  open_count: 1,
  executed_count: 0,
};

describe("/api/orders/modify route", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.xenonFetch.mockReset();
  });

  it("confirms a direct modify from FastAPI /orders without reading orders.json", async () => {
    mocks.xenonFetch
      .mockResolvedValueOnce({ status: "ok", message: "Modified" })
      .mockResolvedValueOnce({ status: "ok" })
      .mockResolvedValueOnce(confirmedOrders);

    const { POST } = await import("../app/api/orders/modify/route");
    const response = await POST(
      new Request("http://localhost/api/orders/modify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orderId: 7001, permId: 9001, newPrice: 5.5 }),
      }),
    );
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual({
      status: "ok",
      message: "Modified",
      orders: confirmedOrders,
    });
    expect(mocks.xenonFetch.mock.calls.map((call) => call[0])).toEqual([
      "/orders/modify",
      "/orders/refresh",
      "/orders",
    ]);
  });

  it("returns refreshed Postgres orders after combo replacement succeeds", async () => {
    mocks.xenonFetch
      .mockResolvedValueOnce({ known: false, state: null })
      .mockResolvedValueOnce({ status: "ok", message: "Cancelled" })
      .mockResolvedValueOnce({
        status: "ok",
        message: "Replacement placed",
        orderId: 7002,
        permId: 9002,
      })
      .mockResolvedValueOnce({ status: "ok" })
      .mockResolvedValueOnce(replacementOrders);

    const { POST } = await import("../app/api/orders/modify/route");
    const response = await POST(
      new Request("http://localhost/api/orders/modify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          orderId: 7001,
          permId: 9001,
          replaceOrder: {
            client_attempt_id: "rep-attempt-1",
            type: "combo",
            symbol: "AAPL",
            action: "SELL",
            quantity: 1,
            limitPrice: 0.75,
            legs: [
              {
                expiry: "20260327",
                strike: 190,
                right: "P",
                action: "SELL",
                ratio: 1,
              },
              {
                expiry: "20260327",
                strike: 200,
                right: "C",
                action: "BUY",
                ratio: 1,
              },
            ],
          },
        }),
      }),
    );
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.orders).toEqual(replacementOrders);
    expect(mocks.xenonFetch.mock.calls.map((call) => call[0])).toEqual([
      "/orders/attempts/rep-attempt-1",
      "/orders/cancel",
      "/orders/place",
      "/orders/refresh",
      "/orders",
    ]);
  });

  it("returns refreshed Postgres orders when replacement placement fails after cancel", async () => {
    mocks.xenonFetch
      .mockResolvedValueOnce({ known: false, state: null })
      .mockResolvedValueOnce({ status: "ok", message: "Cancelled" })
      // A definite rejection (4xx validation); an uncertain one is covered in
      // place-outcome-unknown.test.tsx and never says CRITICAL/re-place.
      .mockRejectedValueOnce(Object.assign(new Error("place rejected"), { status: 400, body: { reason_code: "INVALID_ORDER_BODY" } }))
      .mockResolvedValueOnce({ status: "ok" })
      .mockResolvedValueOnce(replacementOrders);

    const { POST } = await import("../app/api/orders/modify/route");
    const response = await POST(
      new Request("http://localhost/api/orders/modify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          orderId: 7001,
          replaceOrder: {
            client_attempt_id: "rep-attempt-1",
            type: "combo",
            symbol: "AAPL",
            action: "SELL",
            quantity: 1,
            limitPrice: 0.75,
            legs: [
              {
                expiry: "20260327",
                strike: 190,
                right: "P",
                action: "SELL",
                ratio: 1,
              },
              {
                expiry: "20260327",
                strike: 200,
                right: "C",
                action: "BUY",
                ratio: 1,
              },
            ],
          },
        }),
      }),
    );
    const body = await response.json();

    expect(response.status).toBe(502);
    expect(String(body.error)).toContain("CRITICAL");
    expect(body.orders).toEqual(replacementOrders);
    expect(mocks.xenonFetch.mock.calls.map((call) => call[0])).toEqual([
      "/orders/attempts/rep-attempt-1",
      "/orders/cancel",
      "/orders/place",
      "/orders/refresh",
      "/orders",
    ]);
  });

  it.each([
    ["known (any state)", () => mocks.xenonFetch.mockResolvedValueOnce({ known: true, state: "SUBMITTED" }), 409, "ATTEMPT_BODY_MISMATCH"],
    ["lookup fails", () => mocks.xenonFetch.mockRejectedValueOnce(Object.assign(new Error("down"), { status: 503 })), 503, "IB_CONNECTION"],
  ])("a replacement attempt id that is %s refuses with nothing cancelled", async (_label, arrange, status, code) => {
    arrange();
    const { POST } = await import("../app/api/orders/modify/route");
    const response = await POST(
      new Request("http://localhost/api/orders/modify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          orderId: 7001,
          permId: 9001,
          replaceOrder: {
            client_attempt_id: "rep-attempt-1",
            type: "combo",
            symbol: "AAPL",
            action: "SELL",
            quantity: 1,
            limitPrice: 0.75,
            legs: [
              { expiry: "20260327", strike: 190, right: "P", action: "SELL", ratio: 1 },
              { expiry: "20260327", strike: 200, right: "C", action: "BUY", ratio: 1 },
            ],
          },
        }),
      }),
    );
    const body = await response.json();
    expect(response.status).toBe(status);
    expect(body.reason_code).toBe(code);
    expect(String(body.error)).toContain("nothing was cancelled");
    expect(mocks.xenonFetch.mock.calls.map((call) => call[0])).toEqual([
      "/orders/attempts/rep-attempt-1",
    ]);
  });

  it("two consecutive replacements on different orders (fresh ids) both cancel and place", async () => {
    mocks.xenonFetch.mockImplementation(async (path: string) =>
      String(path).startsWith("/orders/attempts/") ? { known: false, state: null } : { status: "ok" },
    );
    const { POST } = await import("../app/api/orders/modify/route");
    for (const [orderId, attemptId] of [[7001, "rep-a"], [7002, "rep-b"]] as const) {
      const response = await POST(
        new Request("http://localhost/api/orders/modify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            orderId,
            replaceOrder: {
              client_attempt_id: attemptId,
              type: "combo",
              symbol: "AAPL",
              action: "SELL",
              quantity: 1,
              limitPrice: 0.75,
              legs: [
                { expiry: "20260327", strike: 190, right: "P", action: "SELL", ratio: 1 },
                { expiry: "20260327", strike: 200, right: "C", action: "BUY", ratio: 1 },
              ],
            },
          }),
        }),
      );
      expect(response.status).toBe(200);
    }
    const calls = mocks.xenonFetch.mock.calls.map((call) => call[0]);
    expect(calls.filter((p) => p === "/orders/cancel")).toHaveLength(2);
    expect(calls.filter((p) => p === "/orders/place")).toHaveLength(2);
    const placed = mocks.xenonFetch.mock.calls
      .filter((call) => call[0] === "/orders/place")
      .map((call) => JSON.parse(String(call[1].body)).client_attempt_id);
    expect(placed).toEqual(["rep-a", "rep-b"]);
  });
});
