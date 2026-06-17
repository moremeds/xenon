import { beforeEach, describe, expect, it, vi } from "vitest";

const mockXenonFetch = vi.fn();

vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: mockXenonFetch,
  internalApiHeaders: (headers: Headers) => {
    const token = process.env.XENON_INTERNAL_API_TOKEN;
    if (token) headers.set("X-Internal-Token", token);
    return headers;
  },
  XenonApiError: class extends Error {
    status: number;
    detail: string;
    body: Record<string, unknown> | null;

    constructor(
      status: number,
      detail: string,
      body: Record<string, unknown> | null = null,
    ) {
      super(`Xenon API ${status}: ${detail}`);
      this.name = "XenonApiError";
      this.status = status;
      this.detail = detail;
      this.body = body;
    }
  },
}));

describe("wizard api routes", () => {
  beforeEach(() => {
    vi.resetModules();
    mockXenonFetch.mockReset();
  });

  it("passes FastAPI wizard errors through without collapsing status", async () => {
    const { XenonApiError } = await import("@/lib/xenonApi");
    mockXenonFetch.mockRejectedValueOnce(
      new XenonApiError(409, "stale wizard state", {
        detail: {
          reason_code: "WIZARD_STALE",
          message: "stale wizard state",
        },
      }),
    );

    const { POST } = await import("../app/api/wizard/plan/route");
    const response = await POST(
      new Request("http://localhost/api/wizard/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker: "AAPL" }),
      }),
    );

    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({
      detail: {
        reason_code: "WIZARD_STALE",
        message: "stale wizard state",
      },
    });
  });

  it("forwards submit bodies to the FastAPI wizard submit route", async () => {
    mockXenonFetch.mockResolvedValueOnce({
      session_id: "wiz-1",
      status: "ok",
    });

    const { POST } =
      await import("../app/api/wizard/sessions/[id]/submit/route");
    const response = await POST(
      new Request("http://localhost/api/wizard/sessions/wiz-1/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_price: "2.45", price_basis: "MID" }),
      }),
      { params: Promise.resolve({ id: "wiz-1" }) },
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      session_id: "wiz-1",
      status: "ok",
    });
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/wizard/sessions/wiz-1/submit",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_price: "2.45", price_basis: "MID" }),
      },
    );
  });

  it("proxies wizard SSE streams as event-stream responses", async () => {
    const encoder = new TextEncoder();
    const upstream = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(
            encoder.encode('event: session\ndata: {"state":"WORKING"}\n\n'),
          );
          controller.close();
        },
      }),
      {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      },
    );

    const fetchSpy = vi.fn().mockResolvedValue(upstream);
    vi.stubGlobal("fetch", fetchSpy);

    const { GET } = await import("../app/api/wizard/stream/route");
    const response = await GET(
      new Request("http://localhost/api/wizard/stream?session_id=wiz-1"),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Type")).toBe("text/event-stream");
    expect(fetchSpy).toHaveBeenCalledWith(
      "http://localhost:8321/wizard/stream?session_id=wiz-1",
      expect.objectContaining({
        cache: "no-store",
      }),
    );
  });
});
