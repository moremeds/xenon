import { beforeEach, describe, expect, it, vi } from "vitest";

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

vi.mock("@/lib/apiContracts", () => ({
  getRequestId: () => "req-test",
  setCacheResponseHeaders: (response: Response) => response,
}));
vi.mock("@/lib/criStaleness", () => ({ isCriDataStale: () => false }));
vi.mock("@/lib/regimeHistory", () => ({ backfillRealizedVolHistory: (items: unknown[]) => items }));

const mocks = vi.hoisted(() => ({
  readFile: vi.fn(),
  xenonFetch: vi.fn(),
}));

vi.mock("fs/promises", () => ({ readFile: mocks.readFile }));
vi.mock("@/lib/xenonApi", () => ({ xenonFetch: mocks.xenonFetch }));

describe("scan cache routes", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.readFile.mockReset();
    mocks.xenonFetch.mockReset();
    mocks.readFile.mockImplementation(async (path: string) => {
      throw new Error(`scan routes must not read runtime JSON cache: ${path}`);
    });
  });

  it("scanner GET reads FastAPI /scan", async () => {
    mocks.xenonFetch.mockResolvedValueOnce({ scan_id: "scan-1", candidates: [] });

    const { GET } = await import("../app/api/scanner/route");
    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.scan_id).toBe("scan-1");
    expect(mocks.readFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/scan",
      expect.objectContaining({ method: "GET", timeout: 10_000 }),
    );
  });

  it("discover GET reads FastAPI /discover", async () => {
    mocks.xenonFetch.mockResolvedValueOnce({ discovery_time: "2026-04-28T15:00:00Z", candidates_found: 1, candidates: [] });

    const { GET } = await import("../app/api/discover/route");
    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.candidates_found).toBe(1);
    expect(mocks.readFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/discover",
      expect.objectContaining({ method: "GET", timeout: 10_000 }),
    );
  });

  it("regime GET reads FastAPI /regime", async () => {
    mocks.xenonFetch.mockResolvedValueOnce({
      scan_time: "2026-04-28T15:00:00Z",
      date: "2026-04-28",
      cri: { score: 2, level: "LOW", components: {} },
      history: [],
      spy_closes: [],
    });

    const { GET } = await import("../app/api/regime/route");
    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.cri.score).toBe(2);
    expect(mocks.readFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/regime",
      expect.objectContaining({ method: "GET", timeout: 10_000 }),
    );
  });
});
