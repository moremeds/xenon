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
  readDataFile: vi.fn(),
  xenonFetch: vi.fn(),
}));

vi.mock("@tools/data-reader", () => ({ readDataFile: mocks.readDataFile }));
vi.mock("@tools/schemas/ib-orders", () => ({ OrdersData: {} }));
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
    mocks.readDataFile.mockReset();
    mocks.xenonFetch.mockReset();
    mocks.readDataFile.mockImplementation(async (path: string) => {
      throw new Error(`modify route must not read data/orders.json: ${path}`);
    });
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
    expect(mocks.readDataFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch.mock.calls.map((call) => call[0])).toEqual([
      "/orders/modify",
      "/orders/refresh",
      "/orders",
    ]);
  });

  it("returns refreshed Postgres orders after combo replacement succeeds", async () => {
    mocks.xenonFetch
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
    expect(mocks.readDataFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch.mock.calls.map((call) => call[0])).toEqual([
      "/orders/cancel",
      "/orders/place",
      "/orders/refresh",
      "/orders",
    ]);
  });

  it("returns refreshed Postgres orders when replacement placement fails after cancel", async () => {
    mocks.xenonFetch
      .mockResolvedValueOnce({ status: "ok", message: "Cancelled" })
      .mockRejectedValueOnce(new Error("place rejected"))
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
    expect(mocks.readDataFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch.mock.calls.map((call) => call[0])).toEqual([
      "/orders/cancel",
      "/orders/place",
      "/orders/refresh",
      "/orders",
    ]);
  });
});
