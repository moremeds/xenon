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
  writeFile: vi.fn(),
  xenonFetch: vi.fn(),
}));

vi.mock("fs/promises", () => ({
  readFile: mocks.readFile,
  writeFile: mocks.writeFile,
}));
vi.mock("@/lib/xenonApi", () => ({ xenonFetch: mocks.xenonFetch }));

describe("/api/journal/sync route", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.readFile.mockReset();
    mocks.writeFile.mockReset();
    mocks.xenonFetch.mockReset();
    mocks.readFile.mockImplementation(async (path: string) => {
      throw new Error(`journal sync must not read legacy files: ${path}`);
    });
  });

  it("proxies FastAPI journal sync without reading reconciliation or trade_log files", async () => {
    mocks.xenonFetch.mockResolvedValueOnce({
      imported: 0,
      skipped: 0,
      pending_outbox: 2,
    });

    const { POST } = await import("../app/api/journal/sync/route");
    const response = await POST();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual({ imported: 0, skipped: 0, pending_outbox: 2 });
    expect(mocks.readFile).not.toHaveBeenCalled();
    expect(mocks.writeFile).not.toHaveBeenCalled();
    expect(mocks.xenonFetch).toHaveBeenCalledWith(
      "/journal/sync",
      expect.objectContaining({ method: "POST", timeout: 10_000 }),
    );
  });
});
