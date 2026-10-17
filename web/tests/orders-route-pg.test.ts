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

const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => ({ xenonFetch: mockXenonFetch }));

const ordersPayload = {
  last_sync: "2026-03-10T15:00:00Z",
  open_orders: [{ orderId: 7001, symbol: "AAPL" }],
  executed_orders: [{ execId: "fill-1", symbol: "AAPL" }],
  open_count: 1,
  executed_count: 1,
};

describe("/api/orders route", () => {
  beforeEach(() => {
    vi.resetModules();
    mockXenonFetch.mockReset();
  });

  it("GET proxies FastAPI /orders without reading orders.json", async () => {
    mockXenonFetch.mockResolvedValueOnce(ordersPayload);

    const { GET } = await import("../app/api/orders/route");
    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual(ordersPayload);
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/orders",
      expect.objectContaining({ method: "GET", timeout: 10_000 }),
    );
  });

  it("POST refreshes through FastAPI then reads /orders from Postgres", async () => {
    mockXenonFetch
      .mockResolvedValueOnce({ status: "ok" })
      .mockResolvedValueOnce(ordersPayload);

    const { POST } = await import("../app/api/orders/route");
    const response = await POST();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.open_count).toBe(1);
    expect(mockXenonFetch.mock.calls.map((call) => call[0])).toEqual([
      "/orders/refresh",
      "/orders",
    ]);
  });
});
