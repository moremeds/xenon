import { beforeEach, expect, test, vi } from "vitest";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { NextRequest } from "next/server";
import { buildEvaluateCommand, __resolvePiInput } from "../app/api/pi/route";

const { mockXenonFetch } = vi.hoisted(() => ({
  mockXenonFetch: vi.fn(),
}));

vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: mockXenonFetch,
}));

type CommandResult = {
  status: number;
  stdout: string;
  stderr: string;
};

const projectRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
);

const runPython = (args: string[]): CommandResult => {
  // Entry-point binaries under .venv/bin/ are spawned directly; legacy .py
  // scripts are invoked via python3.13.
  const [first, ...rest] = args;
  const isEntryPoint =
    first.includes(".venv/bin/") || first.startsWith("xenon-");
  const result = isEntryPoint
    ? spawnSync(first, rest, {
        cwd: projectRoot,
        encoding: "utf8",
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
      })
    : spawnSync("python3.13", args, {
        cwd: projectRoot,
        encoding: "utf8",
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
      });

  return {
    status: result.status ?? 1,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
  };
};

const runPiRequest = async (input: string) => {
  const { POST } = await import("../app/api/pi/route");
  const req = new NextRequest("http://localhost/api/pi", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input }),
  });

  const response = await POST(req);
  const body = await response.json();
  return { response, body };
};

beforeEach(() => {
  mockXenonFetch.mockReset();
});

test("__resolvePiInput combines command and separate text payloads", () => {
  expect(__resolvePiInput({ command: "/evaluate", text: "KWEB" })).toBe(
    "/evaluate KWEB",
  );
  expect(__resolvePiInput({ command: "/evaluate", input: "KWEB" })).toBe(
    "/evaluate KWEB",
  );
  expect(__resolvePiInput({ input: "/evaluate KWEB" })).toBe("/evaluate KWEB");
  expect(__resolvePiInput({ text: "/evaluate KWEB" })).toBe("/evaluate KWEB");
});

test("buildEvaluateCommand builds single evaluate command with default and explicit --days", () => {
  expect(buildEvaluateCommand(["AAPL"])).toEqual([
    ".venv/bin/xenon-evaluate",
    "AAPL",
    "--days",
    "5",
  ]);
  expect(buildEvaluateCommand(["AAPL", "--days", "10"])).toEqual([
    ".venv/bin/xenon-evaluate",
    "AAPL",
    "--days",
    "10",
  ]);
});

test("pi API returns portfolio payload from FastAPI", async () => {
  mockXenonFetch.mockResolvedValue({
    bankroll: 100000,
    position_count: 0,
    positions: [],
  });

  const { response, body } = await runPiRequest("/portfolio");

  expect(response.status).toBe(200);
  expect(body.command).toBe("portfolio");
  expect(body.status).toBe("ok");
  expect(body.source).toBe("api");
  expect(mockXenonFetch).toHaveBeenCalledWith("/portfolio", {
    method: "GET",
    timeout: 10_000,
  });
  expect(typeof body.output === "string").toBeTruthy();
  expect(body.output.includes("bankroll")).toBeTruthy();
});

test("pi API returns journal entries with limit", async () => {
  const { response, body } = await runPiRequest("/journal --limit 2");

  expect(response.status).toBe(200);
  expect(body.command).toBe("journal");
  expect(body.status).toBe("ok");
  const parsed = JSON.parse(body.output);
  expect(Array.isArray(parsed.trades)).toBeTruthy();
  expect(parsed.trades.length <= 2).toBeTruthy();
});

test("pi API blocks unsupported commands", async () => {
  const { response, body } = await runPiRequest("rm -rf /");

  expect(response.status).toBe(400);
  expect(typeof body.error).toBe("string");
});

test("assistant API route returns mock response when mock mode is enabled", async () => {
  const prev = process.env.ASSISTANT_MOCK;
  process.env.ASSISTANT_MOCK = "1";
  try {
    // Dynamic import after setting env so isMockMode() sees it
    const mod = await import("../app/api/assistant/route");
    const req = new NextRequest("http://localhost/api/assistant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: [{ role: "user", content: "analyze brze" }],
      }),
    });

    const response = await mod.POST(req);
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(typeof body.content).toBe("string");
    expect(body.content).toContain("Mock Claude response");
    expect(body.model).toBe("mock");
  } finally {
    if (prev === undefined) delete process.env.ASSISTANT_MOCK;
    else process.env.ASSISTANT_MOCK = prev;
  }
}, 15_000);

test("pi command --help screens are available", () => {
  const helpCommands = [
    { command: [".venv/bin/xenon-fetch-flow", "--help"], expectedStatus: 0 },
    { command: [".venv/bin/xenon-discover", "--help"], expectedStatus: 0 },
    { command: [".venv/bin/xenon-scan", "--help"], expectedStatus: 0 },
    { command: [".venv/bin/xenon-fetch-ticker"], expectedStatus: 2 },
  ];

  for (const item of helpCommands) {
    const result = runPython(item.command);
    expect(result.status).toBe(item.expectedStatus);
    const text = `${result.stdout} ${result.stderr}`.toLowerCase();
    expect(text.includes("usage") || text.includes("description")).toBeTruthy();
  }
}, 15_000);

test("kelly command returns valid risk sizing JSON", () => {
  const result = runPython([
    ".venv/bin/xenon-kelly",
    "--prob",
    "0.35",
    "--odds",
    "3.5",
    "--fraction",
    "0.25",
    "--bankroll",
    "100000",
  ]);

  expect(result.status).toBe(0);

  const payload = JSON.parse(result.stdout);
  expect(payload.recommendation).toBe("STRONG");
  expect(typeof payload.full_kelly_pct).toBe("number");
  expect(typeof payload.fractional_kelly_pct).toBe("number");
  expect(payload.use_size > 0).toBeTruthy();
});

test("GET /api/prices returns deprecation response", async () => {
  const { GET } = await import("../app/api/prices/route");

  const response = await GET();
  const body = (await response.json()) as { error?: string };

  expect(response.status).toBe(405);
  expect(typeof body.error === "string").toBeTruthy();
  expect(body.error!.includes("deprecated")).toBeTruthy();
});

test("POST /api/prices returns deprecation response", async () => {
  const { POST } = await import("../app/api/prices/route");
  const request = new NextRequest("http://localhost/api/prices", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });

  const response = await POST(request);
  const body = (await response.json()) as { error?: string };

  expect(response.status).toBe(405);
  expect(body.error).toContain("deprecated");
});
