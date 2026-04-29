import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * Tests for the FastAPI migration — verifies the new xenonFetch-based routes
 * handle success, failure, and cache fallback correctly.
 *
 * Covers:
 * - Scanner/Discover/FlowAnalysis POST: success → fresh data, failure → cached data
 * - Portfolio POST: success → data, failure → cached fallback
 * - Orders POST: coalescing, success → data, failure → cached fallback
 * - Cancel/Modify: input validation preserved, error propagation
 * - Attribution GET: success → data, failure → 500
 * - Blotter POST: success → data, failure → cached data when available
 * - Options chain/expirations: success → data, missing symbol → 400
 */

// ---------------------------------------------------------------------------
// Mocks — must be declared before imports (vi.mock is hoisted)
// ---------------------------------------------------------------------------

// Mock xenonFetch — the ONLY external dependency for migrated routes
const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: mockXenonFetch,
  XenonApiError: class extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail: string) {
      super(`Xenon API ${status}: ${detail}`);
      this.name = "XenonApiError";
      this.status = status;
      this.detail = detail;
    }
  },
}));

// Mock @tools/data-reader for routes that read cached files
const mockReadDataFile = vi
  .fn()
  .mockResolvedValue({ ok: false, error: "not found" });
vi.mock("@tools/data-reader", () => ({
  readDataFile: mockReadDataFile,
}));

// Mock @tools/schemas/ib-orders and ib-sync (TypeBox schemas)
vi.mock("@tools/schemas/ib-orders", () => ({ OrdersData: {} }));
vi.mock("@tools/schemas/ib-sync", () => ({ PortfolioData: {} }));

// Mock fs/promises for routes that read/write cache files
const mockReadFile = vi.fn();
const mockWriteFile = vi.fn().mockResolvedValue(undefined);
const mockStat = vi
  .fn()
  .mockResolvedValue({ mtimeMs: Date.now() - 5_000, mtime: new Date() });
const mockStatSync = vi.fn().mockReturnValue({ mtime: new Date() });
vi.mock("fs/promises", () => ({
  readFile: mockReadFile,
  writeFile: mockWriteFile,
  stat: mockStat,
  readdir: vi.fn().mockResolvedValue([]),
  mkdir: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("fs", () => ({
  statSync: mockStatSync,
}));

// Mock criStaleness for regime route
vi.mock("@/lib/criStaleness", () => ({
  isCriDataStale: vi.fn().mockReturnValue(false),
}));
vi.mock("@/lib/criCache", () => ({
  selectPreferredCriCandidate: vi.fn().mockReturnValue(null),
}));
vi.mock("@/lib/regimeHistory", () => ({
  backfillRealizedVolHistory: vi.fn().mockReturnValue([]),
}));
vi.mock("@/lib/performanceFreshness", () => ({
  isPerformanceBehindPortfolioSync: vi.fn().mockReturnValue(false),
  isPortfolioBehindCurrentEtSession: vi.fn().mockReturnValue(false),
}));

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function makeRequest(url: string, init?: RequestInit): Request {
  return new Request(url, init);
}

beforeEach(() => {
  vi.resetModules();
  mockXenonFetch.mockReset();
  mockReadDataFile.mockReset();
  mockReadFile.mockReset();
  mockWriteFile.mockReset();
  mockStat.mockReset();
  mockStatSync.mockReset();
  // Default: fresh stat so no background sync triggers
  mockStat.mockResolvedValue({
    mtimeMs: Date.now() - 5_000,
    mtime: new Date(),
  });
  mockStatSync.mockReturnValue({ mtime: new Date() });
});

// =============================================================================
// POST /api/discover — FastAPI proxy
// =============================================================================

describe("POST /api/discover (via xenonFetch)", () => {
  it("returns discovery data on success", async () => {
    const data = {
      discovery_time: "2026-03-14",
      candidates_found: 12,
      candidates: [],
    };
    mockXenonFetch.mockResolvedValue(data);
    mockStatSync.mockReturnValue({ mtime: new Date() });

    const { POST } = await import("../app/api/discover/route");
    const res = await POST();
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.candidates_found).toBe(12);
  });

  it("returns 502 on failure without JSON cache fallback", async () => {
    mockXenonFetch.mockRejectedValue(new Error("timeout"));

    const { POST } = await import("../app/api/discover/route");
    const res = await POST();
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toContain("timeout");
    expect(mockReadFile).not.toHaveBeenCalled();
  });
});

// =============================================================================
// POST /api/flow-analysis — success + cache fallback
// =============================================================================

describe("POST /api/flow-analysis (via xenonFetch)", () => {
  const ibReq = () =>
    new Request("http://localhost/api/flow-analysis?account=ib", {
      method: "POST",
    });
  const futuReq = () =>
    new Request("http://localhost/api/flow-analysis?account=futu", {
      method: "POST",
    });

  it("returns flow data on success and forwards account", async () => {
    const data = {
      analysis_time: "2026-03-14",
      account: "ib",
      positions_scanned: 20,
      supports: [],
      against: [],
    };
    mockXenonFetch.mockResolvedValue(data);
    mockStatSync.mockReturnValue({ mtime: new Date() });

    const { POST } = await import("../app/api/flow-analysis/route");
    const res = await POST(ibReq());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.positions_scanned).toBe(20);
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/flow-analysis?account=ib",
      expect.any(Object),
    );
  });

  it("forwards futu account to xenonFetch", async () => {
    mockXenonFetch.mockResolvedValue({
      analysis_time: "x",
      account: "futu",
      positions_scanned: 23,
    });
    mockStatSync.mockReturnValue({ mtime: new Date() });

    const { POST } = await import("../app/api/flow-analysis/route");
    await POST(futuReq());
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/flow-analysis?account=futu",
      expect.any(Object),
    );
  });

  it("rejects unknown account with 400", async () => {
    const { POST } = await import("../app/api/flow-analysis/route");
    const res = await POST(
      new Request("http://localhost/api/flow-analysis?account=etrade", {
        method: "POST",
      }),
    );
    expect(res.status).toBe(400);
  });

  it("returns empty 200 with warning header when xenonFetch fails", async () => {
    // Post-overhaul: no disk cache fallback; the route returns an empty
    // payload with an X-Sync-Warning header so the UI shows a stale notice.
    mockXenonFetch.mockRejectedValue(new Error("502"));

    const { POST } = await import("../app/api/flow-analysis/route");
    const res = await POST(ibReq());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.positions_scanned).toBe(0);
    expect(res.headers.get("X-Sync-Warning")).toContain("unavailable");
  });

  it("returns empty 200 when both xenonFetch and cache fail", async () => {
    mockXenonFetch.mockRejectedValue(new Error("502"));
    mockReadFile.mockRejectedValue(
      Object.assign(new Error("ENOENT"), { code: "ENOENT" }),
    );
    mockStatSync.mockImplementation(() => {
      throw new Error("ENOENT");
    });

    const { POST } = await import("../app/api/flow-analysis/route");
    const res = await POST(futuReq());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.positions_scanned).toBe(0);
    expect(body.account).toBe("futu");
    expect(res.headers.get("X-Sync-Warning")).toContain("unavailable");
  });
});

describe("GET /api/flow-analysis", () => {
  it("returns empty 200 when cache file is missing", async () => {
    mockReadFile.mockRejectedValue(
      Object.assign(new Error("ENOENT"), { code: "ENOENT" }),
    );
    mockStatSync.mockImplementation(() => {
      throw new Error("ENOENT");
    });

    const { GET } = await import("../app/api/flow-analysis/route");
    const res = await GET(
      new Request("http://localhost/api/flow-analysis?account=futu"),
    );
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.positions_scanned).toBe(0);
    expect(body.account).toBe("futu");
  });

  it("rejects unknown account with 400", async () => {
    const { GET } = await import("../app/api/flow-analysis/route");
    const res = await GET(
      new Request("http://localhost/api/flow-analysis?account=etrade"),
    );
    expect(res.status).toBe(400);
  });
});

// =============================================================================
// GET /api/attribution — via xenonFetch
// =============================================================================

describe("GET /api/attribution (via xenonFetch)", () => {
  it("returns attribution data on success", async () => {
    mockXenonFetch.mockResolvedValue({
      total_trades: 39,
      total_realized_pnl: 126927,
    });

    const { GET } = await import("../app/api/attribution/route");
    const res = await GET();
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.total_trades).toBe(39);
  });

  it("returns 500 on xenonFetch failure", async () => {
    mockXenonFetch.mockRejectedValue(new Error("Script exited with code 1"));

    const { GET } = await import("../app/api/attribution/route");
    const res = await GET();
    expect(res.status).toBe(500);
    const body = await res.json();
    expect(body.error).toContain("Script exited");
  });
});

// =============================================================================
// POST /api/portfolio — via xenonFetch + cache fallback
// =============================================================================

describe("POST /api/portfolio (via xenonFetch)", () => {
  it("returns synced data on success", async () => {
    const portfolio = {
      bankroll: 100000,
      last_sync: "2026-03-14T14:30:00",
      positions: [],
    };
    mockXenonFetch.mockResolvedValue(portfolio);

    const { POST } = await import("../app/api/portfolio/route");
    const res = await POST();
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.bankroll).toBe(100000);
  });

  it("returns 502 when sync fails (no JSON-file fallback after PG migration)", async () => {
    // Phase 1 of the postgres read-path migration deliberately removed the
    // file-cache fallback — silently serving stale paper data when the live
    // sync fails was the bug. Failures must surface loudly.
    mockXenonFetch.mockRejectedValue(new Error("IB connection refused"));

    const { POST } = await import("../app/api/portfolio/route");
    const res = await POST();
    expect(res.status).toBe(502);
  });
});

// =============================================================================
// GET /api/portfolio — proxies to FastAPI GET /portfolio (PG-backed)
// =============================================================================

describe("GET /api/portfolio", () => {
  it("returns FastAPI data on success", async () => {
    const data = {
      bankroll: 100000,
      last_sync: new Date().toISOString(),
      positions: [],
    };
    mockXenonFetch.mockResolvedValueOnce(data);

    const { GET } = await import("../app/api/portfolio/route");
    const res = await GET();
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.bankroll).toBe(100000);
  });

  it("returns 404 when FastAPI reports no snapshot exists yet", async () => {
    const notFound = Object.assign(new Error("no snapshot"), { status: 404 });
    mockXenonFetch
      .mockRejectedValueOnce(notFound) // GET /portfolio
      .mockResolvedValueOnce({ ok: true }); // POST /portfolio/background-sync (triggered)

    const { GET } = await import("../app/api/portfolio/route");
    const res = await GET();
    expect(res.status).toBe(404);
  });
});

// =============================================================================
// POST /api/orders — via xenonFetch, no JSON-file fallback
// =============================================================================

describe("POST /api/orders (via xenonFetch)", () => {
  it("returns refreshed orders on success", async () => {
    const orders = {
      last_sync: "2026-03-14",
      open_orders: [],
      executed_orders: [],
      open_count: 0,
      executed_count: 0,
    };
    mockXenonFetch
      .mockResolvedValueOnce({ status: "ok" })
      .mockResolvedValueOnce(orders);

    const { POST } = await import("../app/api/orders/route");
    const res = await POST();
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.open_count).toBe(0);
  });

  it("returns 502 when sync fails instead of serving cached orders", async () => {
    mockXenonFetch.mockRejectedValue(new Error("timeout"));

    const { POST } = await import("../app/api/orders/route");
    const res = await POST();
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toContain("timeout");
    expect(res.headers.get("X-Sync-Warning")).toBeNull();
  });

  it("returns 502 when refresh succeeds but the PG read fails", async () => {
    mockXenonFetch
      .mockResolvedValueOnce({ status: "ok" })
      .mockRejectedValueOnce(new Error("timeout"));

    const { POST } = await import("../app/api/orders/route");
    const res = await POST();
    expect(res.status).toBe(502);
  });
});

// =============================================================================
// POST /api/orders/cancel — input validation preserved
// =============================================================================

describe("POST /api/orders/cancel (via xenonFetch)", () => {
  it("returns 400 when both orderId and permId are missing", async () => {
    const { POST } = await import("../app/api/orders/cancel/route");
    const req = makeRequest("http://localhost/api/orders/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const res = await POST(req);
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toContain("orderId");
  });

  it("succeeds when orderId is provided", async () => {
    const ordersPayload = { open_orders: [], executed_orders: [] };
    mockXenonFetch
      .mockResolvedValueOnce({ status: "ok", message: "Cancelled" }) // cancel
      .mockResolvedValueOnce({}) // refresh
      .mockResolvedValueOnce(ordersPayload); // PG orders read

    const { POST } = await import("../app/api/orders/cancel/route");
    const req = makeRequest("http://localhost/api/orders/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orderId: 123 }),
    });
    const res = await POST(req);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.status).toBe("ok");
    expect(body.orders).toEqual(ordersPayload);
    expect(mockReadDataFile).not.toHaveBeenCalled();
    expect(mockXenonFetch.mock.calls.map((call) => call[0])).toEqual([
      "/orders/cancel",
      "/orders/refresh",
      "/orders",
    ]);
  });
});

// =============================================================================
// POST /api/orders/modify — input validation preserved
// =============================================================================

describe("POST /api/orders/modify (via xenonFetch)", () => {
  it("returns 400 when both orderId and permId are missing", async () => {
    const { POST } = await import("../app/api/orders/modify/route");
    const req = makeRequest("http://localhost/api/orders/modify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ newPrice: 10.0 }),
    });
    const res = await POST(req);
    expect(res.status).toBe(400);
  });

  it("returns 400 when newPrice is missing", async () => {
    const { POST } = await import("../app/api/orders/modify/route");
    const req = makeRequest("http://localhost/api/orders/modify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orderId: 123 }),
    });
    const res = await POST(req);
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toContain("newPrice");
  });

  it("returns 400 when newPrice is zero", async () => {
    const { POST } = await import("../app/api/orders/modify/route");
    const req = makeRequest("http://localhost/api/orders/modify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orderId: 123, newPrice: 0 }),
    });
    const res = await POST(req);
    expect(res.status).toBe(400);
  });

  it("returns 400 when newPrice is negative", async () => {
    const { POST } = await import("../app/api/orders/modify/route");
    const req = makeRequest("http://localhost/api/orders/modify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orderId: 123, newPrice: -5 }),
    });
    const res = await POST(req);
    expect(res.status).toBe(400);
  });
});

// =============================================================================
// POST /api/orders/place — input validation + IB rejection detection
// =============================================================================

describe("POST /api/orders/place (via xenonFetch)", () => {
  const client_attempt_id = "test-fastapi-migration";

  it("returns 400 when symbol is missing", async () => {
    const { POST } = await import("../app/api/orders/place/route");
    const req = makeRequest("http://localhost/api/orders/place", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "BUY", quantity: 10, limitPrice: 150 }),
    });
    const res = await POST(req);
    expect(res.status).toBe(400);
  });

  it("detects IB silent rejection (Cancelled status)", async () => {
    mockXenonFetch.mockResolvedValueOnce({
      status: "ok",
      orderId: 42,
      permId: 9999,
      initialStatus: "Cancelled",
      message: "BUY 10 AAPL @ $150",
    });

    const { POST } = await import("../app/api/orders/place/route");
    const req = makeRequest("http://localhost/api/orders/place", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        type: "stock",
        symbol: "AAPL",
        action: "BUY",
        quantity: 10,
        limitPrice: 150,
        client_attempt_id,
      }),
    });
    const res = await POST(req);
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toContain("rejected");
  });

  it("detects IB silent rejection (Unknown status)", async () => {
    mockXenonFetch.mockResolvedValueOnce({
      status: "ok",
      orderId: 42,
      permId: 9999,
      initialStatus: "Unknown",
    });

    const { POST } = await import("../app/api/orders/place/route");
    const req = makeRequest("http://localhost/api/orders/place", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        type: "stock",
        symbol: "AAPL",
        action: "BUY",
        quantity: 10,
        limitPrice: 150,
        client_attempt_id,
      }),
    });
    const res = await POST(req);
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toContain("no acknowledgement");
  });

  it("succeeds with valid stock order", async () => {
    mockXenonFetch
      .mockResolvedValueOnce({
        status: "ok",
        orderId: 42,
        permId: 9999,
        initialStatus: "Submitted",
        message: "BUY 10 AAPL @ $150.00 — Submitted",
      })
      .mockResolvedValueOnce({}); // orders refresh
    mockReadDataFile.mockResolvedValue({
      ok: true,
      data: { open_orders: [], executed_orders: [] },
    });

    const { POST } = await import("../app/api/orders/place/route");
    const req = makeRequest("http://localhost/api/orders/place", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        type: "stock",
        symbol: "AAPL",
        action: "BUY",
        quantity: 10,
        limitPrice: 150,
        client_attempt_id,
      }),
    });
    const res = await POST(req);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.orderId).toBe(42);
    expect(body.initialStatus).toBe("Submitted");
  });

  it("normalizes CALL/PUT combo legs to C/P for FastAPI payload", async () => {
    mockXenonFetch
      .mockResolvedValueOnce({
        status: "ok",
        orderId: 99,
        permId: 100,
        initialStatus: "Submitted",
      })
      .mockResolvedValueOnce({});
    mockReadDataFile
      .mockResolvedValueOnce({ ok: true, data: { positions: [] } })
      .mockResolvedValueOnce({
        ok: true,
        data: { open_orders: [], executed_orders: [] },
      });

    const { POST } = await import("../app/api/orders/place/route");
    const req = makeRequest("http://localhost/api/orders/place", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        type: "combo",
        symbol: "AAPL",
        action: "BUY",
        quantity: 1,
        limitPrice: 2.5,
        client_attempt_id,
        legs: [
          {
            symbol: "AAPL",
            secType: "OPT",
            expiry: "20260417",
            strike: 100,
            right: "CALL",
            action: "BUY",
            ratio: 1,
          },
          {
            symbol: "AAPL",
            secType: "OPT",
            expiry: "20260417",
            strike: 110,
            right: "CALL",
            action: "SELL",
            ratio: 1,
          },
        ],
      }),
    });
    const res = await POST(req);
    expect(res.status).toBe(200);

    const placeCall = mockXenonFetch.mock.calls.find(
      (c) => c[0] === "/orders/place",
    );
    expect(placeCall).toBeDefined();
    const payload = JSON.parse((placeCall![1] as { body: string }).body) as {
      legs: { right: string; symbol?: string }[];
    };
    expect(payload.legs[0].right).toBe("C");
    expect(payload.legs[1].right).toBe("C");
    expect(payload.legs[0].symbol).toBeUndefined();
  });
});

// =============================================================================
// POST /api/blotter — via xenonFetch
// =============================================================================

describe("POST /api/blotter (via xenonFetch)", () => {
  it("returns blotter data on success", async () => {
    const data = {
      as_of: "2026-03-14",
      summary: { closed_trades: 5 },
      closed_trades: [],
      open_trades: [],
    };
    mockXenonFetch.mockResolvedValue(data);

    const { POST } = await import("../app/api/blotter/route");
    const res = await POST();
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.summary.closed_trades).toBe(5);
  });

  it("returns 502 on failure without JSON cache fallback", async () => {
    mockXenonFetch.mockRejectedValue(new Error("Flex query timed out"));

    const { POST } = await import("../app/api/blotter/route");
    const res = await POST();
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toContain("timed out");
    expect(mockReadFile).not.toHaveBeenCalled();
  });
});

// =============================================================================
// GET /api/options/chain — via xenonFetch
// =============================================================================

describe("GET /api/options/chain (via xenonFetch)", () => {
  it("returns 400 when symbol is missing", async () => {
    const { GET } = await import("../app/api/options/chain/route");
    const req = makeRequest("http://localhost/api/options/chain");
    const res = await GET(req);
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toContain("symbol");
  });

  it("returns chain data on success", async () => {
    mockXenonFetch.mockResolvedValue({
      symbol: "AAPL",
      expirations: ["2026-04-17"],
      calls: [],
      puts: [],
    });

    const { GET } = await import("../app/api/options/chain/route");
    const req = makeRequest("http://localhost/api/options/chain?symbol=AAPL");
    const res = await GET(req);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.symbol).toBe("AAPL");
  });

  it("passes expiry parameter when provided", async () => {
    mockXenonFetch.mockResolvedValue({ symbol: "AAPL", calls: [], puts: [] });

    const { GET } = await import("../app/api/options/chain/route");
    const req = makeRequest(
      "http://localhost/api/options/chain?symbol=AAPL&expiry=2026-04-17",
    );
    await GET(req);

    expect(mockXenonFetch).toHaveBeenCalledWith(
      expect.stringContaining("expiry=2026-04-17"),
      expect.any(Object),
    );
  });
});

// =============================================================================
// GET /api/options/expirations — via xenonFetch
// =============================================================================

describe("GET /api/options/expirations (via xenonFetch)", () => {
  it("returns 400 when symbol is missing", async () => {
    const { GET } = await import("../app/api/options/expirations/route");
    const req = makeRequest("http://localhost/api/options/expirations");
    const res = await GET(req);
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toContain("symbol");
  });

  it("returns expirations on success", async () => {
    mockXenonFetch.mockResolvedValue({
      symbol: "GOOG",
      expirations: ["2026-04-17", "2026-05-15"],
    });

    const { GET } = await import("../app/api/options/expirations/route");
    const req = makeRequest(
      "http://localhost/api/options/expirations?symbol=GOOG",
    );
    const res = await GET(req);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.symbol).toBe("GOOG");
    expect(body.expirations).toHaveLength(2);
  });

  it("returns 502 when FastAPI is down", async () => {
    mockXenonFetch.mockRejectedValue(new Error("Connection refused"));

    const { GET } = await import("../app/api/options/expirations/route");
    const req = makeRequest(
      "http://localhost/api/options/expirations?symbol=GOOG",
    );
    const res = await GET(req);
    expect(res.status).toBe(502);
  });
});
