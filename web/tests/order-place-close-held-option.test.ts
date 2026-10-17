import { beforeEach, describe, expect, it, vi } from "vitest";

const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: mockXenonFetch,
}));

describe("POST /api/orders/place — closing a held long option", () => {
  beforeEach(() => {
    vi.resetModules();
    mockXenonFetch.mockReset();
  });

  it("allows SELL option payloads that match a held long call contract", async () => {
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
          client_attempt_id: "close-held-option-1",
          expiry: "20270115",
          strike: 17,
          right: "C",
        }),
      }),
    );

    expect(res.status).toBe(200);
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/orders/place",
      expect.objectContaining({
        method: "POST",
      }),
    );

    const body = await res.json();
    expect(body.status).toBe("ok");
    expect(body.orderId).toBe(12345);
  });
});
