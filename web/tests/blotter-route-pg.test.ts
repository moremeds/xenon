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

const mocks = vi.hoisted(() => ({
  readFile: vi.fn(),
  xenonFetch: vi.fn(),
}));

vi.mock("fs/promises", () => ({ readFile: mocks.readFile }));
vi.mock("@/lib/xenonApi", () => ({ xenonFetch: mocks.xenonFetch }));

const blotterPayload = {
  configured: true,
  source: "postgres",
  as_of: "2026-04-28T15:30:00Z",
  summary: { closed_trades: 1, open_trades: 0, total_commissions: 2.5, realized_pnl: 240 },
  closed_trades: [{ symbol: "AAPL", executions: [] }],
  open_trades: [],
};

describe("/api/blotter route", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.readFile.mockReset();
    mocks.xenonFetch.mockReset();
    mocks.readFile.mockImplementation(async (path: string) => {
      throw new Error(`blotter route must not read data/blotter.json: ${path}`);
    });
  });

  it("GET proxies FastAPI /blotter without reading blotter.json", async () => {
    mocks.xenonFetch.mockResolvedValueOnce(blotterPayload);

    const { GET } = await import("../app/api/blotter/route");
    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual(blotterPayload);
    expect(mocks.readFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/blotter",
      expect.objectContaining({ method: "GET", timeout: 10_000 }),
    );
  });
});
