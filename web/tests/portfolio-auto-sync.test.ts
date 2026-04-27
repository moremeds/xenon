import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * Verifies the GET /api/portfolio stale-while-revalidate behavior after the
 * Phase 1 postgres read-path migration: the route now calls FastAPI
 * `GET /portfolio` for the snapshot and decides staleness from the response's
 * `last_sync` field, not the local file mtime.
 */

// Mock FastAPI client.
const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => ({ xenonFetch: mockXenonFetch }));

// Mock fs (only trade_log.json reads remain).
const mockReadFile = vi.fn();
vi.mock("fs/promises", () => ({ readFile: mockReadFile }));

/** A minimal valid PortfolioData object */
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

/** Returns an ISO timestamp that is `ageMs` milliseconds in the past */
function ageAgo(ageMs: number): string {
  return new Date(Date.now() - ageMs).toISOString();
}

describe("GET /api/portfolio — stale-while-revalidate background sync", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    mockReadFile.mockResolvedValue("[]"); // empty trade log
  });

  it("triggers FastAPI background sync when last_sync is >60 s old", async () => {
    const portfolio = makePortfolio(ageAgo(90_000));
    mockXenonFetch
      .mockResolvedValueOnce(portfolio) // GET /portfolio
      .mockResolvedValueOnce({ ok: true }); // POST /portfolio/background-sync

    const { GET } = await import("../app/api/portfolio/route");
    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.last_sync).toBe(portfolio.last_sync);
    expect(mockXenonFetch).toHaveBeenCalledTimes(2);
    expect(mockXenonFetch.mock.calls[0]?.[0]).toBe("/portfolio");
    expect(mockXenonFetch.mock.calls[1]?.[0]).toBe(
      "/portfolio/background-sync",
    );
  });

  it("does NOT trigger FastAPI sync when last_sync is <60 s old (fresh)", async () => {
    const portfolio = makePortfolio(ageAgo(10_000));
    mockXenonFetch.mockResolvedValueOnce(portfolio);

    const { GET } = await import("../app/api/portfolio/route");
    await GET();

    expect(mockXenonFetch).toHaveBeenCalledOnce();
    expect(mockXenonFetch.mock.calls[0]?.[0]).toBe("/portfolio");
  });

  it("triggers background sync when FastAPI returns 404 (no snapshot yet)", async () => {
    const notFound = Object.assign(new Error("not found"), { status: 404 });
    mockXenonFetch
      .mockRejectedValueOnce(notFound) // GET /portfolio → 404
      .mockResolvedValueOnce({ ok: true }); // POST /portfolio/background-sync

    const { GET } = await import("../app/api/portfolio/route");
    const response = await GET();

    expect(response.status).toBe(404);
    expect(mockXenonFetch).toHaveBeenCalledTimes(2);
    expect(mockXenonFetch.mock.calls[1]?.[0]).toBe(
      "/portfolio/background-sync",
    );
  });

  it("does not trigger a second sync when one is already in-flight", async () => {
    const portfolio = makePortfolio(ageAgo(90_000));
    let bgCalls = 0;
    mockXenonFetch.mockImplementation((path: string) => {
      if (path === "/portfolio") return Promise.resolve(portfolio);
      // Background sync — never-resolving so the in-flight guard stays armed.
      bgCalls += 1;
      return new Promise(() => {});
    });

    const { GET } = await import("../app/api/portfolio/route");

    await GET();
    await GET();

    expect(bgCalls).toBe(1);
  });
});
