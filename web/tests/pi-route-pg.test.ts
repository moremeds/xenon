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
  NextRequest: Request,
}));

const mocks = vi.hoisted(() => ({
  readFile: vi.fn(),
  xenonFetch: vi.fn(),
}));

vi.mock("node:fs/promises", () => ({ readFile: mocks.readFile }));
vi.mock("@/lib/xenonApi", () => ({ xenonFetch: mocks.xenonFetch }));

function piRequest(input: string): Request {
  return new Request("http://localhost/api/pi", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input }),
  });
}

describe("/api/pi route PG-backed reads", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.readFile.mockReset();
    mocks.xenonFetch.mockReset();
    mocks.readFile.mockImplementation(async (path: string) => {
      throw new Error(`PI route must not read legacy JSON: ${path}`);
    });
  });

  it("formats portfolio data from FastAPI without reading portfolio.json", async () => {
    mocks.xenonFetch.mockResolvedValueOnce({
      bankroll: 50000,
      position_count: 1,
      defined_risk_count: 1,
      undefined_risk_count: 0,
      last_sync: "2026-04-28T15:00:00Z",
      positions: [{ ticker: "AAPL", structure: "Long Call", entry_cost: 1234 }],
    });

    const { POST } = await import("../app/api/pi/route");
    const response = await POST(piRequest("/portfolio") as never);
    const body = await response.json();
    const output = JSON.parse(body.output);

    expect(response.status).toBe(200);
    expect(body.command).toBe("portfolio");
    expect(output.bankroll).toBe(50000);
    expect(output.positions[0].ticker).toBe("AAPL");
    expect(mocks.readFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/portfolio",
      expect.objectContaining({ method: "GET", timeout: 10_000 }),
    );
  });

  it("formats limited journal data from FastAPI without reading trade_log.json", async () => {
    mocks.xenonFetch.mockResolvedValueOnce({
      trades: [
        { id: 2, ticker: "MSFT", decision: "CLOSED" },
        { id: 1, ticker: "AAPL", decision: "OPEN" },
      ],
    });

    const { POST } = await import("../app/api/pi/route");
    const response = await POST(piRequest("/journal --limit 1") as never);
    const body = await response.json();
    const output = JSON.parse(body.output);

    expect(response.status).toBe(200);
    expect(body.command).toBe("journal");
    expect(output.trades).toEqual([{ id: 2, ticker: "MSFT", decision: "CLOSED" }]);
    expect(mocks.readFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/journal?limit=1",
      expect.objectContaining({ method: "GET", timeout: 10_000 }),
    );
  });
});
