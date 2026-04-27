import { beforeEach, describe, expect, it, vi } from "vitest";

const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: mockXenonFetch,
}));

const mockReadDataFile = vi.fn();
vi.mock("@tools/data-reader", () => ({
  readDataFile: mockReadDataFile,
}));

vi.mock("@tools/schemas/ib-orders", () => ({
  OrdersData: {},
}));

describe("POST /api/orders/place — closing a held long option", () => {
  beforeEach(() => {
    vi.resetModules();
    mockXenonFetch.mockReset();
    mockReadDataFile.mockReset();
  });

  it("allows SELL option payloads that match a held long call contract", async () => {
    mockReadDataFile.mockResolvedValueOnce({
      ok: true,
      data: {
        open_orders: [],
        executed_orders: [],
        open_count: 0,
        executed_count: 0,
      },
    });

    mockXenonFetch
      .mockResolvedValueOnce({
        positions: [
          {
            ticker: "WULF",
            expiry: "2027-01-15",
            structure_type: "Long Call",
            contracts: 77,
            direction: "LONG",
            legs: [
              { direction: "LONG", type: "Call", contracts: 77, strike: 17 },
            ],
          },
        ],
      })
      .mockResolvedValueOnce({
        status: "ok",
        orderId: 12345,
        permId: 54321,
        initialStatus: "Submitted",
        message: "Order placed successfully",
      })
      .mockResolvedValueOnce({});

    const { POST } = await import("../app/api/orders/place/route");
    const res = await POST(
      new Request("http://localhost/api/orders/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          type: "option",
          symbol: "WULF",
          action: "SELL",
          quantity: 77,
          limitPrice: 4.47,
          expiry: "20270115",
          strike: 17,
          right: "C",
        }),
      }),
    );

    expect(res.status).toBe(200);
    expect(mockXenonFetch).toHaveBeenNthCalledWith(
      1,
      "/portfolio",
      expect.objectContaining({
        method: "GET",
      }),
    );
    expect(mockXenonFetch).toHaveBeenNthCalledWith(
      2,
      "/orders/place",
      expect.objectContaining({
        method: "POST",
      }),
    );
    expect(mockReadDataFile).not.toHaveBeenCalledWith(
      "data/portfolio.json",
      expect.anything(),
    );

    const body = await res.json();
    expect(body.status).toBe("ok");
    expect(body.orderId).toBe(12345);
  });
});
