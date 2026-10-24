/**
 * Futu account_env proxy forwarding (plan v4 §11).
 */
import { describe, expect, it, vi, beforeEach } from "vitest";

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

class FakeXenonApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(`Xenon API ${status}: ${detail}`);
    this.name = "XenonApiError";
  }
}

vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: mockXenonFetch,
  XenonApiError: FakeXenonApiError,
}));

function req(url: string): Request {
  return new Request(url);
}

describe("Futu proxy account_env forwarding", () => {
  beforeEach(() => {
    mockXenonFetch.mockReset();
  });

  it("GET /api/futu/portfolio forwards paper account_env", async () => {
    mockXenonFetch.mockResolvedValueOnce({ ok: true, positions: [] });
    const { GET } = await import("../app/api/futu/portfolio/route");
    await GET(
      req("http://test/api/futu/portfolio?broker=FUTU&account_env=paper"),
    );
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/futu/portfolio?broker=FUTU&account_env=paper",
      { method: "GET" },
    );
  });

  it("GET /api/futu/portfolio forwards live account_env", async () => {
    mockXenonFetch.mockResolvedValueOnce({ ok: true, positions: [] });
    const { GET } = await import("../app/api/futu/portfolio/route");
    await GET(
      req("http://test/api/futu/portfolio?broker=FUTU&account_env=live"),
    );
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/futu/portfolio?broker=FUTU&account_env=live",
      { method: "GET" },
    );
  });

  it("POST /api/orders refresh without account_env reaches FastAPI bare", async () => {
    mockXenonFetch
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ open_orders: [] });
    const { POST } = await import("../app/api/orders/route");
    await POST(req("http://test/api/orders?broker=FUTU"));
    expect(mockXenonFetch.mock.calls[0][0]).toBe("/orders/refresh?broker=FUTU");
    expect(mockXenonFetch.mock.calls[1][0]).toBe("/orders?broker=FUTU");
  });

  it("POST /api/orders forwards distinct paper vs live refresh paths", async () => {
    mockXenonFetch.mockResolvedValue({}).mockResolvedValue({ open_orders: [] });
    const { POST } = await import("../app/api/orders/route");
    await POST(req("http://test/api/orders?broker=FUTU&account_env=paper"));
    expect(mockXenonFetch.mock.calls[0][0]).toBe(
      "/orders/refresh?broker=FUTU&account_env=paper",
    );
    mockXenonFetch.mockClear();
    mockXenonFetch.mockResolvedValue({}).mockResolvedValue({ open_orders: [] });
    await POST(req("http://test/api/orders?broker=FUTU&account_env=live"));
    expect(mockXenonFetch.mock.calls[0][0]).toBe(
      "/orders/refresh?broker=FUTU&account_env=live",
    );
  });

  it("GET /api/blotter forwards account_env verbatim", async () => {
    mockXenonFetch.mockResolvedValueOnce({ rows: [] });
    const { GET } = await import("../app/api/blotter/route");
    await GET(req("http://test/api/blotter?broker=FUTU&account_env=live"));
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/blotter?broker=FUTU&account_env=live",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("GET /api/journal forwards account_env verbatim", async () => {
    mockXenonFetch.mockResolvedValueOnce({ trades: [] });
    const { GET } = await import("../app/api/journal/route");
    await GET(req("http://test/api/journal?broker=FUTU&account_env=paper"));
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/journal?broker=FUTU&account_env=paper",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("POST /api/orders/place forwards query without inventing account_env", async () => {
    mockXenonFetch.mockResolvedValueOnce({ orderId: 1 });
    const { POST } = await import("../app/api/orders/place/route");
    const body = {
      type: "stock",
      symbol: "AAPL",
      action: "BUY",
      quantity: 1,
      limitPrice: 100,
      client_attempt_id: "test-attempt-1",
    };
    await POST(
      new Request("http://test/api/orders/place?broker=FUTU", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
    expect(mockXenonFetch.mock.calls[0][0]).toBe("/orders/place?broker=FUTU");
  });
});
