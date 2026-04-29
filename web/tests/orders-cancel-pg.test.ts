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

const ordersPayload = {
  last_sync: "2026-03-10T15:00:00Z",
  open_orders: [{ orderId: 7001, symbol: "AAPL" }],
  executed_orders: [{ execId: "fill-1", symbol: "AAPL" }],
  open_count: 1,
  executed_count: 1,
};

describe("/api/orders/cancel route", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.readDataFile.mockReset();
    mocks.xenonFetch.mockReset();
    mocks.readDataFile.mockImplementation(async (path: string) => {
      throw new Error(`cancel route must not read data/orders.json: ${path}`);
    });
  });

  it("returns refreshed Postgres orders without reading orders.json", async () => {
    mocks.xenonFetch
      .mockResolvedValueOnce({ status: "ok", message: "Cancelled" })
      .mockResolvedValueOnce({ status: "ok" })
      .mockResolvedValueOnce(ordersPayload);

    const { POST } = await import("../app/api/orders/cancel/route");
    const response = await POST(
      new Request("http://localhost/api/orders/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orderId: 7001 }),
      }),
    );
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual({
      status: "ok",
      message: "Cancelled",
      orders: ordersPayload,
    });
    expect(mocks.readDataFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch.mock.calls.map((call) => call[0])).toEqual([
      "/orders/cancel",
      "/orders/refresh",
      "/orders",
    ]);
  });
});
