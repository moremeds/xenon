import { beforeEach, expect, test, vi } from "vitest";
import { NextRequest } from "next/server";

const mockXenonFetch = vi.fn();
const mockReadFile = vi.fn();

vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: mockXenonFetch,
}));

vi.mock("node:fs/promises", () => ({
  readFile: mockReadFile,
}));

vi.mock("node:fs", () => ({
  existsSync: vi.fn((filePath: string) =>
    filePath.endsWith("scripts/infra/dev/run_pytest_affected.py"),
  ),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

const postPi = async (input: string) => {
  const { POST } = await import("../app/api/pi/route");
  const req = new NextRequest("http://localhost/api/pi", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input }),
  });

  return POST(req);
};

test("portfolio command reads portfolio from FastAPI instead of data/portfolio.json", async () => {
  mockXenonFetch.mockResolvedValue({
    bankroll: 25000,
    position_count: 1,
    defined_risk_count: 1,
    undefined_risk_count: 0,
    last_sync: "2026-04-27T12:00:00Z",
    positions: [
      {
        ticker: "AAPL",
        structure: "Call Debit Spread",
        expiry: "2026-06-19",
        risk_profile: "defined",
        entry_cost: 120,
      },
    ],
  });

  const response = await postPi("/portfolio");
  const body = await response.json();

  expect(response.status).toBe(200);
  expect(mockXenonFetch).toHaveBeenCalledWith("/portfolio", {
    method: "GET",
    timeout: 10_000,
  });
  expect(mockReadFile).not.toHaveBeenCalled();
  expect(body.source).toBe("api");
  expect(JSON.parse(body.output)).toMatchObject({
    bankroll: 25000,
    position_count: 1,
    positions: [{ ticker: "AAPL" }],
  });
});
