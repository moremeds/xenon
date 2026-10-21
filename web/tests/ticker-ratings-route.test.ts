/**
 * GET /api/ticker/ratings — proxies FastAPI GET /ticker/ratings via xenonFetch.
 *
 * Fixture: real Unusual Whales `screener/analysts` response for MSFT captured
 * once on 2026-10-21 (shared with the pytest route test; see its _provenance).
 * No network at test time: xenonFetch is mocked.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => {
  class XenonApiError extends Error {
    constructor(
      public readonly status: number,
      public readonly detail: string,
      public readonly body: Record<string, unknown> | null = null,
    ) {
      super(`Xenon API ${status}: ${detail}`);
      this.name = "XenonApiError";
    }
  }
  return { xenonFetch: mockXenonFetch, XenonApiError };
});

const loadRoute = async () => {
  const { GET } = await import("../app/api/ticker/ratings/route");
  const { XenonApiError } = await import("@/lib/xenonApi");
  return { GET, XenonApiError };
};

const fixture = JSON.parse(
  readFileSync(
    resolve(
      __dirname,
      "../../src/xenon/api/tests/fixtures/uw_analysts_msft_2026-10-21.json",
    ),
    "utf8",
  ),
) as { data: Array<Record<string, string>> };

const req = (qs = "") =>
  new Request(`http://localhost/api/ticker/ratings${qs}`);

describe("GET /api/ticker/ratings", () => {
  // Braces matter: a returned mock would be run by vitest as a cleanup hook.
  beforeEach(() => {
    mockXenonFetch.mockReset();
  });

  it("400s without calling FastAPI when ticker is missing or blank", async () => {
    const { GET } = await loadRoute();
    for (const qs of ["", "?ticker=", "?ticker=%20"]) {
      const res = await GET(req(qs));
      expect(res.status).toBe(400);
      expect((await res.json()).error).toContain("ticker");
    }
    expect(mockXenonFetch).not.toHaveBeenCalled();
  });

  it("calls FastAPI /ticker/ratings with the uppercased, encoded ticker", async () => {
    const { GET } = await loadRoute();
    mockXenonFetch.mockResolvedValue({
      ticker: "BRK.B",
      source: "unusual_whales",
      as_of: "2026-10-21T12:00:00Z",
      ratings: [],
    });
    await GET(req("?ticker=brk.b"));
    expect(mockXenonFetch.mock.calls[0][0]).toBe("/ticker/ratings?ticker=BRK.B");
  });

  it("maps raw UW rows onto the RatingsTab recent_changes shape", async () => {
    const { GET } = await loadRoute();
    mockXenonFetch.mockResolvedValue({
      ticker: "MSFT",
      source: "unusual_whales",
      as_of: "2026-10-21T12:00:00Z",
      ratings: fixture.data,
    });
    const res = await GET(req("?ticker=MSFT"));
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toMatchObject({
      ticker: "MSFT",
      source: "unusual_whales",
      as_of: "2026-10-21T12:00:00Z",
    });
    expect(body.recent_changes).toHaveLength(fixture.data.length);
    expect(body.recent_changes[0]).toEqual({
      date: "2026-10-20",
      firm: "BMO Capital",
      action: "maintained",
      to_grade: "buy",
      target: 600,
    });
    const upgrade = body.recent_changes.find(
      (c: { action: string }) => c.action === "upgraded",
    );
    expect(upgrade).toMatchObject({ firm: "Stifel", to_grade: "buy", target: 575 });
    // no derived scoring fields
    expect(body.recommendation).toBeUndefined();
    expect(body.ratings).toBeUndefined();
  });

  it("returns 502 with reason_code when FastAPI reports an upstream failure", async () => {
    const { GET, XenonApiError } = await loadRoute();
    mockXenonFetch.mockRejectedValue(
      new XenonApiError(502, "Unusual Whales analyst ratings unavailable", {
        reason_code: "UW_UPSTREAM_ERROR",
      }),
    );
    const res = await GET(req("?ticker=MSFT"));
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({
      error: "Unusual Whales analyst ratings unavailable",
      reason_code: "UW_UPSTREAM_ERROR",
    });
  });

  it("propagates a FastAPI 400 (invalid ticker) as 400", async () => {
    const { GET, XenonApiError } = await loadRoute();
    mockXenonFetch.mockRejectedValue(
      new XenonApiError(400, "ticker must match [A-Z0-9.-]{1,10}", {
        reason_code: "INVALID_TICKER",
      }),
    );
    const res = await GET(req("?ticker=bad%20one"));
    expect(res.status).toBe(400);
    expect((await res.json()).reason_code).toBe("INVALID_TICKER");
  });

  it("returns 502 when FastAPI is unreachable", async () => {
    const { GET } = await loadRoute();
    mockXenonFetch.mockRejectedValue(new Error("fetch failed"));
    const res = await GET(req("?ticker=MSFT"));
    expect(res.status).toBe(502);
    expect((await res.json()).error).toBe("fetch failed");
  });
});
