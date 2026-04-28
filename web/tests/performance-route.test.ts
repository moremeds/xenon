import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

const mockReadFile = vi.fn();
const mockStat = vi.fn();

vi.mock("fs/promises", () => ({
  readFile: mockReadFile,
  stat: mockStat,
}));

const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => ({ xenonFetch: mockXenonFetch }));

const originalOpenTtl = process.env.XENON_PERFORMANCE_TTL_OPEN_S;
const originalClosedTtl = process.env.XENON_PERFORMANCE_TTL_CLOSED_S;

describe("/api/performance route", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-03-13T16:00:00Z"));
    vi.clearAllMocks();
    mockReadFile.mockReset();
    mockStat.mockReset();
    mockReadFile.mockImplementation(async (path: string) => {
      throw new Error(`route must not read JSON files: ${path}`);
    });
    mockStat.mockImplementation(async (path: string) => {
      throw new Error(`route must not stat JSON files: ${path}`);
    });
    delete process.env.XENON_PERFORMANCE_TTL_OPEN_S;
    delete process.env.XENON_PERFORMANCE_TTL_CLOSED_S;
  });

  afterEach(() => {
    vi.useRealTimers();
    if (originalOpenTtl === undefined) {
      delete process.env.XENON_PERFORMANCE_TTL_OPEN_S;
    } else {
      process.env.XENON_PERFORMANCE_TTL_OPEN_S = originalOpenTtl;
    }
    if (originalClosedTtl === undefined) {
      delete process.env.XENON_PERFORMANCE_TTL_CLOSED_S;
    } else {
      process.env.XENON_PERFORMANCE_TTL_CLOSED_S = originalClosedTtl;
    }
  });

  it("GET cold start fetches performance from FastAPI without reading JSON files", async () => {
    mockXenonFetch.mockResolvedValueOnce({
      as_of: "2026-03-13",
      last_sync: "2026-03-13T16:00:00Z",
      summary: { total_return: 0.18 },
      series: [],
    });

    const { GET } = await import("../app/api/performance/route");
    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.summary.total_return).toBe(0.18);
    expect(mockReadFile).not.toHaveBeenCalled();
    expect(mockStat).not.toHaveBeenCalled();
    expect(mockXenonFetch).toHaveBeenCalledOnce();
    expect(mockXenonFetch).toHaveBeenCalledWith(
      "/performance",
      expect.objectContaining({ method: "POST", timeout: 180_000 }),
    );
  });

  it("GET serves the route cache within the open-market TTL", async () => {
    mockXenonFetch.mockResolvedValueOnce({
      as_of: "2026-03-13",
      last_sync: "2026-03-13T16:00:00Z",
      summary: { sharpe_ratio: 1.2 },
      series: [],
    });

    const { GET } = await import("../app/api/performance/route");
    await GET();

    vi.advanceTimersByTime(4 * 60_000);
    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.summary.sharpe_ratio).toBe(1.2);
    expect(mockXenonFetch).toHaveBeenCalledOnce();
    expect(mockReadFile).not.toHaveBeenCalled();
    expect(mockStat).not.toHaveBeenCalled();
  });

  it("GET returns stale route cache and refreshes in the background after the open-market TTL", async () => {
    mockXenonFetch
      .mockResolvedValueOnce({
        as_of: "2026-03-13",
        last_sync: "2026-03-13T16:00:00Z",
        summary: { ending_equity: 100_000 },
        series: [],
      })
      .mockResolvedValueOnce({
        as_of: "2026-03-13",
        last_sync: "2026-03-13T16:06:00Z",
        summary: { ending_equity: 101_000 },
        series: [],
      });

    const { GET } = await import("../app/api/performance/route");
    await GET();

    vi.advanceTimersByTime(6 * 60_000);
    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.summary.ending_equity).toBe(100_000);
    expect(mockXenonFetch).toHaveBeenCalledTimes(2);
    expect(mockXenonFetch).toHaveBeenNthCalledWith(
      2,
      "/performance",
      expect.objectContaining({ method: "POST", timeout: 180_000 }),
    );
  });

  it("GET uses the closed-market TTL environment override", async () => {
    process.env.XENON_PERFORMANCE_TTL_CLOSED_S = "120";
    vi.setSystemTime(new Date("2026-03-14T15:00:00Z"));
    mockXenonFetch
      .mockResolvedValueOnce({
        as_of: "2026-03-13",
        last_sync: "2026-03-13T21:00:00Z",
        summary: { total_return: 0.12 },
        series: [],
      })
      .mockResolvedValueOnce({
        as_of: "2026-03-13",
        last_sync: "2026-03-14T15:02:01Z",
        summary: { total_return: 0.13 },
        series: [],
      });

    const { GET } = await import("../app/api/performance/route");
    await GET();

    vi.advanceTimersByTime(119_000);
    await GET();
    expect(mockXenonFetch).toHaveBeenCalledOnce();

    vi.advanceTimersByTime(2_000);
    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.summary.total_return).toBe(0.12);
    expect(mockXenonFetch).toHaveBeenCalledTimes(2);
  });

  it("GET cold start returns 502 when FastAPI generation fails", async () => {
    mockXenonFetch.mockRejectedValueOnce(new Error("FastAPI down"));

    const { GET } = await import("../app/api/performance/route");
    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(502);
    expect(body.error).toContain("FastAPI down");
    expect(mockReadFile).not.toHaveBeenCalled();
    expect(mockStat).not.toHaveBeenCalled();
  });

  it("POST refreshes performance through FastAPI and updates the route cache", async () => {
    mockXenonFetch
      .mockResolvedValueOnce({
        as_of: "2026-03-13",
        last_sync: "2026-03-13T16:00:00Z",
        summary: { sharpe_ratio: 1.84 },
        series: [],
      })
      .mockResolvedValueOnce({
        as_of: "2026-03-13",
        last_sync: "2026-03-13T16:01:00Z",
        summary: { sharpe_ratio: 1.91 },
        series: [],
      });

    const { GET, POST } = await import("../app/api/performance/route");
    await GET();

    const postRes = await POST();
    const postBody = await postRes.json();
    const cachedRes = await GET();
    const cachedBody = await cachedRes.json();

    expect(postRes.status).toBe(200);
    expect(postBody.summary.sharpe_ratio).toBe(1.91);
    expect(cachedBody.summary.sharpe_ratio).toBe(1.91);
    expect(mockXenonFetch).toHaveBeenCalledTimes(2);
    expect(mockXenonFetch).toHaveBeenNthCalledWith(
      2,
      "/performance",
      expect.objectContaining({ method: "POST", timeout: 190_000 }),
    );
  });
});
