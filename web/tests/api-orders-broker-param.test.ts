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

const mocks = vi.hoisted(() => ({ xenonFetch: vi.fn() }));
vi.mock("@/lib/xenonApi", () => ({ xenonFetch: mocks.xenonFetch }));

describe("broker param forwarding through Next API routes", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.xenonFetch.mockReset();
    mocks.xenonFetch.mockResolvedValue({
      open_orders: [],
      executed_orders: [],
    });
  });

  it("GET /api/orders?broker=FUTU forwards ?broker=FUTU to FastAPI", async () => {
    const { GET } = await import("@/app/api/orders/route");
    await GET(new Request("http://x/api/orders?broker=FUTU"));
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/orders?broker=FUTU",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("GET /api/orders without broker omits the query", async () => {
    const { GET } = await import("@/app/api/orders/route");
    await GET(new Request("http://x/api/orders"));
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/orders",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("POST /api/orders?broker=FUTU refreshes then reads, both broker-scoped", async () => {
    const { POST } = await import("@/app/api/orders/route");
    await POST(
      new Request("http://x/api/orders?broker=FUTU", { method: "POST" }),
    );
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/orders/refresh?broker=FUTU",
      expect.objectContaining({ method: "POST" }),
    );
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/orders?broker=FUTU",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("GET /api/blotter?broker=FUTU forwards to FastAPI", async () => {
    const { GET } = await import("@/app/api/blotter/route");
    await GET(new Request("http://x/api/blotter?broker=FUTU"));
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/blotter?broker=FUTU",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("GET /api/journal?broker=FUTU forwards to FastAPI", async () => {
    const { GET } = await import("@/app/api/journal/route");
    await GET(new Request("http://x/api/journal?broker=FUTU"));
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/journal?broker=FUTU",
      expect.objectContaining({ method: "GET" }),
    );
  });
});
