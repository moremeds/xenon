import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/server", () => ({
  NextResponse: {
    json: (body: unknown, init?: ResponseInit) =>
      new Response(JSON.stringify(body), {
        ...init,
        headers: { "content-type": "application/json" },
      }),
  },
}));

const mocks = vi.hoisted(() => ({
  readFile: vi.fn(),
  xenonFetch: vi.fn(),
}));

vi.mock("fs/promises", () => ({ readFile: mocks.readFile }));
vi.mock("@/lib/xenonApi", () => {
  class XenonApiError extends Error {
    constructor(
      public readonly status: number,
      public readonly detail: string,
    ) {
      super(detail);
    }
  }
  return { xenonFetch: mocks.xenonFetch, XenonApiError };
});

async function callGet() {
  const { GET } = await import("@/app/api/flex-token/route");
  const res = await GET();
  return { status: res.status, body: await res.json() };
}

describe("GET /api/flex-token", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.readFile.mockReset();
    mocks.xenonFetch.mockReset();
    mocks.readFile.mockImplementation(async (p: string) => {
      throw new Error(`flex-token must not read files: ${p}`);
    });
  });

  it("adapts the FastAPI /health flex_token into the banner shape", async () => {
    mocks.xenonFetch.mockResolvedValueOnce({
      status: "ok",
      flex_token: {
        configured: true,
        expires_at: "2026-10-25T12:00:00+00:00",
        days_remaining: 5,
        reminder_days: [30, 14, 7, 1],
        active_threshold: 7,
        should_warn: true,
        expired: false,
      },
    });
    const { status, body } = await callGet();
    expect(status).toBe(200);
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/health",
      expect.objectContaining({ method: "GET" }),
    );
    expect(mocks.readFile).not.toHaveBeenCalled();
    expect(body).toMatchObject({
      days_remaining: 5,
      expires_at: "2026-10-25T12:00:00+00:00",
      should_warn: true,
      expired: false,
      active_threshold: 7,
      reminder_days: [30, 14, 7, 1],
    });
    expect(body.renewal_url).toContain("interactivebrokers.com");
    expect(body.breadcrumb).toContain("Flex Queries");
  });

  it("reports expired tokens", async () => {
    mocks.xenonFetch.mockResolvedValueOnce({
      flex_token: {
        configured: true,
        expires_at: "2026-10-20T12:00:00+00:00",
        days_remaining: 0,
        reminder_days: [30, 14, 7, 1],
        active_threshold: 1,
        should_warn: true,
        expired: true,
      },
    });
    const { body } = await callGet();
    expect(body.expired).toBe(true);
    expect(body.should_warn).toBe(true);
  });

  it("returns the quiet no-data shape when the token is not configured", async () => {
    mocks.xenonFetch.mockResolvedValueOnce({ flex_token: { configured: false } });
    const { status, body } = await callGet();
    expect(status).toBe(200);
    expect(body).toMatchObject({
      days_remaining: null,
      should_warn: false,
      expired: false,
    });
  });

  it("propagates a FastAPI failure status instead of masking it", async () => {
    const { XenonApiError } = await import("@/lib/xenonApi");
    mocks.xenonFetch.mockRejectedValueOnce(new XenonApiError(503, "api down"));
    const { status, body } = await callGet();
    expect(status).toBe(503);
    expect(body.error).toBe("api down");
  });

  it("returns 502 when FastAPI is unreachable", async () => {
    mocks.xenonFetch.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    const { status } = await callGet();
    expect(status).toBe(502);
  });
});
