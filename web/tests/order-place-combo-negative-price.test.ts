import { beforeEach, describe, expect, it, vi } from "vitest";

const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: mockXenonFetch,
}));

describe("POST /api/orders/place — signed combo prices", () => {
  beforeEach(() => {
    vi.resetModules();
    mockXenonFetch.mockReset();
  });

  it("allows a negative combo limit price and forwards the sign unchanged", async () => {
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
          type: "combo",
          symbol: "IWM",
          action: "SELL",
          quantity: 50,
          limitPrice: -0.4,
          tif: "DAY",
          client_attempt_id: "combo-negative-1",
          legs: [
            { expiry: "20260326", strike: 247, right: "C", action: "BUY", ratio: 1 },
            { expiry: "20260326", strike: 243, right: "P", action: "SELL", ratio: 1 },
          ],
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

    const forwarded = JSON.parse(mockXenonFetch.mock.calls[0][1].body as string);
    expect(forwarded.type).toBe("combo");
    expect(forwarded.symbol).toBe("IWM");
    expect(forwarded.limitPrice).toBe(-0.4);
  });
});
