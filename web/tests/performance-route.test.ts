/**
 * /api/performance proxy route tests.
 *
 * Contract (post perf-rebuild):
 *   - GET reads ?broker= from NextRequest, defaults to IB.
 *   - Forwards verbatim to FastAPI `/performance?broker=<b>` (GET, not POST).
 *   - No second-level cache in the Next route — FastAPI owns the TTL cache
 *     (market-aware 60s open / 30min closed). Duplicating the cache here
 *     caused stale-after-write surprises.
 *   - Preserves upstream HTTPException status (409 cross-env, 503 OpenD down).
 */
import { describe, expect, it, vi } from "vitest";

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

function makeRequest(url: string): { nextUrl: URL } {
  return { nextUrl: new URL(url) };
}

describe("/api/performance proxy route", () => {
  it("GET defaults broker=IB and forwards verbatim", async () => {
    mockXenonFetch.mockResolvedValueOnce({
      status: "ok",
      summary: { total_return: 0.05 },
      series: [],
    });
    const { GET } = await import("../app/api/performance/route");
    const res = await GET(makeRequest("http://test/api/performance") as never);
    const body = await res.json();
    expect(res.status).toBe(200);
    expect(body.summary.total_return).toBe(0.05);
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/performance?broker=IB",
      expect.objectContaining({ timeout: 180_000 }),
    );
    mockXenonFetch.mockReset();
  });

  it("GET forwards ?broker=FUTU", async () => {
    mockXenonFetch.mockResolvedValueOnce({
      status: "ok",
      summary: {},
      series: [],
    });
    const { GET } = await import("../app/api/performance/route");
    await GET(makeRequest("http://test/api/performance?broker=FUTU") as never);
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/performance?broker=FUTU",
      expect.anything(),
    );
    mockXenonFetch.mockReset();
  });

  it("GET preserves upstream 409 cross-env conflict", async () => {
    mockXenonFetch.mockRejectedValueOnce(
      new FakeXenonApiError(409, "NAV account_env conflict"),
    );
    const { GET } = await import("../app/api/performance/route");
    const res = await GET(makeRequest("http://test/api/performance") as never);
    expect(res.status).toBe(409);
    const body = await res.json();
    expect(body.detail ?? body.error).toContain("conflict");
    mockXenonFetch.mockReset();
  });

  it("GET preserves upstream 503 Futu OpenD unreachable", async () => {
    mockXenonFetch.mockRejectedValueOnce(
      new FakeXenonApiError(503, "Futu OpenD unreachable"),
    );
    const { GET } = await import("../app/api/performance/route");
    const res = await GET(
      makeRequest("http://test/api/performance?broker=FUTU") as never,
    );
    expect(res.status).toBe(503);
    mockXenonFetch.mockReset();
  });

  it("GET returns 502 on non-XenonApiError network failures", async () => {
    mockXenonFetch.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    const { GET } = await import("../app/api/performance/route");
    const res = await GET(makeRequest("http://test/api/performance") as never);
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toContain("ECONNREFUSED");
    mockXenonFetch.mockReset();
  });
});
