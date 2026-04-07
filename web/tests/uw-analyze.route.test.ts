import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@clerk/nextjs/server", () => ({
  auth: vi.fn(async () => ({ getToken: async () => "test-token" })),
}));

vi.mock("@/lib/xenonApi", async () => {
  const actual = await vi.importActual<typeof import("../lib/xenonApi")>("../lib/xenonApi");
  return {
    ...actual,
    xenonFetch: vi.fn(),
  };
});

import { xenonFetch, XenonApiError } from "../lib/xenonApi";
import type { UwAnalyzeResponse } from "../lib/types/uwAnalyze";

const mockXenonFetch = vi.mocked(xenonFetch);

const FIXTURE: UwAnalyzeResponse = {
  report: {
    ticker: "AAPL",
    price: 184.22,
    fetched_at: "2026-04-08T14:02:11",
    data_freshness: { gex: "live" },
    scores: {
      market_structure: 24,
      volatility: 19,
      flow: 17,
      positioning: 0,
      composite: 15,
      grade: "B",
      bias: "MIXED",
      mode: "full",
      reweighted: true,
      skipped_buckets: ["positioning"],
    },
    notes: [],
    setup_thesis: {
      bias: "MIXED",
      regime: "R1",
      structure_family: "neutral",
      rationale: "demo",
    },
  },
  display: {
    sector: "XLK",
    iv_rank: 38,
    iv: 22,
    rv: 18.6,
    call_wall_strike: 190,
    put_wall_strike: 175,
    gamma_per_1pct: 42_000_000,
    net_call_premium: 12_400_000,
    net_put_premium: -3_100_000,
    short_volume_ratio: 0.41,
    short_volume_trend: [0.4, 0.41, 0.42],
    term_structure_label: "normal",
    gex_flip: null,
    gex_by_strike: [
      {
        strike: 190,
        call_gamma: 44.8,
        put_gamma: -2.7,
        net_gamma: 42.1,
        distance_pct: 0.031,
        is_call_wall: true,
        is_put_wall: false,
      },
    ],
  },
  generated_at: "2026-04-08T18:00:00Z",
};

async function importRoute() {
  return import(`../app/api/uw-analyze/route?t=${Date.now()}`);
}

describe("POST /api/uw-analyze", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("returns 200 with the upstream response on success", async () => {
    mockXenonFetch.mockResolvedValueOnce(FIXTURE as never);
    const { POST } = await importRoute();
    const req = new Request("http://localhost/api/uw-analyze", {
      method: "POST",
      body: JSON.stringify({ ticker: "AAPL" }),
      headers: { "Content-Type": "application/json" },
    });
    const res = await POST(req);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.report.ticker).toBe("AAPL");
    expect(body.display.sector).toBe("XLK");
    expect(body.display.gex_by_strike[0].is_call_wall).toBe(true);
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/uw-analyze",
      expect.objectContaining({
        method: "POST",
        token: "test-token",
        body: JSON.stringify({ ticker: "AAPL" }),
      }),
    );
  });

  it("returns 400 when ticker is missing", async () => {
    const { POST } = await importRoute();
    const req = new Request("http://localhost/api/uw-analyze", {
      method: "POST",
      body: JSON.stringify({}),
      headers: { "Content-Type": "application/json" },
    });
    const res = await POST(req);
    expect(res.status).toBe(400);
    expect(mockXenonFetch).not.toHaveBeenCalled();
  });

  it("propagates upstream 404 status and detail", async () => {
    mockXenonFetch.mockRejectedValueOnce(new XenonApiError(404, "ticker not found: ZZZZ"));
    const { POST } = await importRoute();
    const req = new Request("http://localhost/api/uw-analyze", {
      method: "POST",
      body: JSON.stringify({ ticker: "ZZZZ" }),
      headers: { "Content-Type": "application/json" },
    });
    const res = await POST(req);
    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body.error).toBe("ticker not found: ZZZZ");
  });

  it("propagates upstream 502 status", async () => {
    mockXenonFetch.mockRejectedValueOnce(new XenonApiError(502, "UW upstream failed"));
    const { POST } = await importRoute();
    const req = new Request("http://localhost/api/uw-analyze", {
      method: "POST",
      body: JSON.stringify({ ticker: "AAPL" }),
      headers: { "Content-Type": "application/json" },
    });
    const res = await POST(req);
    expect(res.status).toBe(502);
  });
});
