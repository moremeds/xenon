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

  // PR-07 fix3: FastAPI /orders/replace owns claim → cancel → place. The
  // Next route is a thin forward: body and status unchanged.
  const replaceOrder = {
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
  };
  const cancelOrders = [{ orderId: 7001, permId: 9001 }];

  async function postReplace() {
    const { POST } = await import("../app/api/orders/modify/route");
    return POST(
      new Request("http://localhost/api/orders/modify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orderId: 7001, permId: 9001, cancelOrders, replaceOrder }),
      }),
    );
  }

  it("forwards a replacement to FastAPI /orders/replace in one call, success body unchanged", async () => {
    const upstream = { status: "ok", orderId: 7002, permId: 9002, submission_id: "s-1" };
    mocks.xenonFetch.mockResolvedValueOnce(upstream);

    const response = await postReplace();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(upstream);
    expect(mocks.xenonFetch).toHaveBeenCalledTimes(1);
    const [path, init] = mocks.xenonFetch.mock.calls[0];
    expect(path).toBe("/orders/replace");
    expect(JSON.parse(String(init.body))).toEqual({ orderId: 7001, permId: 9001, cancelOrders, replaceOrder });
  });

  it.each([
    [409, { reason_code: "REPLACEMENT_IN_PROGRESS", detail: "in progress", state: "PENDING" }],
    [409, { reason_code: "ORDER_NOT_OWNED", detail: { message: "x" }, replacement_placed: false, cancelled: [] }],
    [400, { reason_code: "ETF_CALL_UNCOVERED", detail: "naked", error: "CRITICAL: Original order cancelled" }],
    [502, { reason_code: "PLACE_OUTCOME_UNKNOWN", detail: "unknown", error: "status unknown", submission_id: "s" }],
  ])("forwards FastAPI %i body unchanged", async (status, body) => {
    const { XenonApiError } = await import("@/lib/xenonApi");
    mocks.xenonFetch.mockRejectedValueOnce(new XenonApiError(status, "x", body));

    const response = await postReplace();

    expect(response.status).toBe(status);
    expect(await response.json()).toEqual(body);
    expect(mocks.xenonFetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["socket reset", new TypeError("fetch failed")],
    ["timeout", Object.assign(new Error("timed out"), { name: "TimeoutError" })],
  ])("a %s on the forward is uncertain (502 PLACE_OUTCOME_UNKNOWN)", async (_label, err) => {
    mocks.xenonFetch.mockRejectedValueOnce(err);
    const response = await postReplace();
    expect(response.status).toBe(502);
    expect((await response.json()).reason_code).toBe("PLACE_OUTCOME_UNKNOWN");
  });
});
