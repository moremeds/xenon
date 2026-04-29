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
vi.mock("@/lib/gexStaleness", () => ({
  isGexDataStale: () => false,
}));

const mocks = vi.hoisted(() => ({
  readFile: vi.fn(),
  xenonFetch: vi.fn(),
}));

vi.mock("fs/promises", () => ({ readFile: mocks.readFile }));
vi.mock("@/lib/xenonApi", () => ({ xenonFetch: mocks.xenonFetch }));

const gexPayload = {
  scan_time: "2026-04-28T15:30:00Z",
  market_open: true,
  ticker: "SPX",
  net_gex: 123,
  levels: {},
  profile: [],
  history: [],
};

describe("/api/gex route", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.readFile.mockReset();
    mocks.xenonFetch.mockReset();
    mocks.readFile.mockImplementation(async (path: string) => {
      throw new Error(`gex route must not read data/gex.json: ${path}`);
    });
  });

  it("GET proxies FastAPI /gex without reading gex.json", async () => {
    mocks.xenonFetch.mockResolvedValueOnce(gexPayload);

    const { GET } = await import("../app/api/gex/route");
    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.ticker).toBe("SPX");
    expect(body.net_gex).toBe(123);
    expect(mocks.readFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/gex",
      expect.objectContaining({ method: "GET", timeout: 10_000 }),
    );
  });
});
