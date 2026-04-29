import { describe, it, expect, vi, beforeEach } from "vitest";

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

/**
 * sync-fallback.test.ts
 *
 * When sync fails, routes surface the FastAPI/Postgres failure instead of
 * serving local JSON fallback data.
 */

const mockStat = vi.fn();
vi.mock("fs/promises", () => ({ stat: mockStat }));

const mockReadDataFile = vi.fn();
vi.mock("@tools/data-reader", () => ({ readDataFile: mockReadDataFile }));

const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => ({ xenonFetch: mockXenonFetch }));

function makePortfolio(lastSync: string) {
  return {
    bankroll: 100_000,
    peak_value: 100_000,
    last_sync: lastSync,
    positions: [],
    total_deployed_pct: 0,
    total_deployed_dollars: 0,
    remaining_capacity_pct: 100,
    position_count: 0,
    defined_risk_count: 0,
    undefined_risk_count: 0,
    avg_kelly_optimal: null,
  };
}

function makeOrders(lastSync: string) {
  return {
    last_sync: lastSync,
    open_orders: [],
    executed_orders: [],
    open_count: 0,
    executed_count: 0,
  };
}

describe("POST /api/portfolio — sync failure fallback", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    mockStat.mockResolvedValue({ mtimeMs: Date.now() });
  });

  it("returns 502 when ibSync fails (no JSON-file fallback after PG migration)", async () => {
    // Phase 1 of the postgres read-path migration removed the cached-file
    // fallback — silently serving stale paper data when the live sync failed
    // was the original bug. Sync failures must surface loudly.
    mockXenonFetch.mockRejectedValue(new Error("Connect call failed"));

    const { POST } = await import("../app/api/portfolio/route");
    const response = await POST();

    expect(response.status).toBe(502);
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/portfolio/sync",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("does not set X-Sync-Warning header (header retired with cache fallback)", async () => {
    mockXenonFetch.mockRejectedValue(new Error("Connect call failed"));

    const { POST } = await import("../app/api/portfolio/route");
    const response = await POST();

    expect(response.headers.get("X-Sync-Warning")).toBeNull();
  });

  it("returns 502 when sync fails (cache no longer participates)", async () => {
    mockXenonFetch.mockRejectedValue(new Error("Connect call failed"));
    mockReadDataFile.mockResolvedValue({ ok: false, error: "File not found" });

    const { POST } = await import("../app/api/portfolio/route");
    const response = await POST();

    expect(response.status).toBe(502);
  });

  it("returns cached portfolio data with 200 when sync succeeds", async () => {
    const synced = makePortfolio("2026-03-13T15:00:00Z");
    mockXenonFetch.mockResolvedValue(synced);
    mockReadDataFile.mockResolvedValue({ ok: true, data: synced });

    const { POST } = await import("../app/api/portfolio/route");
    const response = await POST();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.last_sync).toBe("2026-03-13T15:00:00Z");
    expect(response.headers.get("X-Sync-Warning")).toBeNull();
  });
});

describe("POST /api/orders — PG-only refresh", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    mockStat.mockResolvedValue({ mtimeMs: Date.now() });
  });

  it("returns 502 when refresh fails instead of serving cached orders", async () => {
    mockXenonFetch.mockRejectedValue(new Error("Connect call failed"));

    const { POST } = await import("../app/api/orders/route");
    const response = await POST();
    const body = await response.json();

    expect(response.status).toBe(502);
    expect(body.error).toContain("Connect call failed");
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/orders/refresh",
      expect.objectContaining({ method: "POST" }),
    );
    expect(mockReadDataFile).not.toHaveBeenCalled();
  });

  it("does not set X-Sync-Warning header because cache fallback is removed", async () => {
    mockXenonFetch.mockRejectedValue(new Error("Connect call failed"));

    const { POST } = await import("../app/api/orders/route");
    const response = await POST();

    expect(response.headers.get("X-Sync-Warning")).toBeNull();
  });

  it("returns 502 when refresh succeeds but the PG orders read fails", async () => {
    mockXenonFetch
      .mockResolvedValueOnce({ status: "ok" })
      .mockRejectedValueOnce(new Error("PG unavailable"));

    const { POST } = await import("../app/api/orders/route");
    const response = await POST();

    expect(response.status).toBe(502);
    expect(mockReadDataFile).not.toHaveBeenCalled();
  });

  it("returns PG orders data with 200 when refresh succeeds", async () => {
    const orders = makeOrders("2026-03-13T15:00:00Z");
    mockXenonFetch
      .mockResolvedValueOnce({ status: "ok" })
      .mockResolvedValueOnce(orders);

    const { POST } = await import("../app/api/orders/route");
    const response = await POST();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.last_sync).toBe("2026-03-13T15:00:00Z");
    expect(response.headers.get("X-Sync-Warning")).toBeNull();
    expect(mockReadDataFile).not.toHaveBeenCalled();
  });
});
