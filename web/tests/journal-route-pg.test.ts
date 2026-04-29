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

const journalPayload = {
  trades: [
    {
      id: 1,
      date: "2026-04-28",
      ticker: "AAPL",
      structure: "Long Call",
      decision: "MANUAL",
    },
  ],
};

describe("/api/journal route", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.readFile.mockReset();
    mocks.xenonFetch.mockReset();
    mocks.readFile.mockImplementation(async (path: string) => {
      throw new Error(`journal route must not read trade_log.json: ${path}`);
    });
  });

  it("proxies FastAPI /journal without reading trade_log.json", async () => {
    mocks.xenonFetch.mockResolvedValueOnce(journalPayload);

    const { GET } = await import("../app/api/journal/route");
    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual(journalPayload);
    expect(mocks.readFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/journal",
      expect.objectContaining({ method: "GET", timeout: 10_000 }),
    );
  });
});
