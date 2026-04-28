import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/server", () => ({
  NextResponse: {
    json: (body: unknown, init?: ResponseInit) =>
      new Response(JSON.stringify(body), {
        status: init?.status ?? 200,
        headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      }),
  },
}));

const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => ({
  XenonApiError: class XenonApiError extends Error {
    status: number;
    detail: unknown;
    constructor(status: number, message: string, detail?: unknown) {
      super(message);
      this.status = status;
      this.detail = detail;
    }
  },
  xenonFetch: mockXenonFetch,
}));

const mockReadDataFile = vi.fn();
vi.mock("@tools/data-reader", () => ({
  readDataFile: mockReadDataFile,
}));

vi.mock("@tools/schemas/ib-orders", () => ({
  OrdersData: {},
}));

describe("POST /api/orders/place — FastAPI owns runtime Gate 4", () => {
  beforeEach(() => {
    vi.resetModules();
    mockXenonFetch.mockReset();
    mockReadDataFile.mockReset();
  });

  it("does not read JSON files before forwarding a BUY stock order", async () => {
    mockXenonFetch
      .mockResolvedValueOnce({
        status: "ok",
        orderId: 12345,
        permId: 54321,
        initialStatus: "Submitted",
        message: "Order placed successfully",
        tif: "GTC",
      })
      .mockResolvedValueOnce({});
    mockReadDataFile.mockImplementation(async (path: string) => {
      if (path === "data/portfolio.json") {
        throw new Error("portfolio JSON guard was called");
      }
      return {
        ok: true,
        data: { open_orders: [], executed_orders: [], open_count: 0, executed_count: 0 },
      };
    });

    const { POST } = await import("../app/api/orders/place/route");
    const res = await POST(
      new Request("http://localhost/api/orders/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          type: "stock",
          symbol: "QQQ",
          action: "BUY",
          quantity: 1,
          limitPrice: 400,
          tif: "GTC",
          client_attempt_id: "buy-stock-1",
        }),
      }),
    );

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.tif).toBe("GTC");
    expect(mockReadDataFile).not.toHaveBeenCalled();
    const forwarded = JSON.parse(mockXenonFetch.mock.calls[0][1].body as string);
    expect(forwarded.client_attempt_id).toBe("buy-stock-1");
    expect(forwarded.action).toBe("BUY");
  });

  it("keeps the Next timeout longer than quote validation plus IB placement", async () => {
    mockXenonFetch
      .mockResolvedValueOnce({
        status: "ok",
        orderId: 12345,
        permId: 54321,
        initialStatus: "Submitted",
        message: "Order placed successfully",
      })
      .mockResolvedValueOnce({});

    const { POST } = await import("../app/api/orders/place/route");
    await POST(
      new Request("http://localhost/api/orders/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          type: "stock",
          symbol: "QQQ",
          action: "BUY",
          quantity: 1,
          limitPrice: 400,
          client_attempt_id: "buy-stock-timeout",
        }),
      }),
    );

    expect(mockXenonFetch.mock.calls[0][0]).toBe("/orders/place");
    expect(mockXenonFetch.mock.calls[0][1].timeout).toBeGreaterThanOrEqual(45_000);
  });
});
