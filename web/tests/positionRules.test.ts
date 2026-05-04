import { afterAll, describe, expect, it, vi } from "vitest";

import { ensureTestFastApi } from "./fastapiHarness";

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

process.env.XENON_TRADING_MODE = "paper";
process.env.XENON_BROKER_ACCOUNT = "DU1234567";
process.env.XENON_BROKER = "IB";
process.env.DATABASE_URL =
  process.env.DATABASE_URL_TEST ??
  "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test";

const fastApiHarness = await ensureTestFastApi();
const fastApiIt = fastApiHarness.available ? it : it.skip;

if (!fastApiHarness.available && fastApiHarness.skipReason) {
  console.warn(
    `[positionRules] Skipping FastAPI-backed tests: ${fastApiHarness.skipReason}`,
  );
}

afterAll(async () => {
  await fastApiHarness.close();
});

describe("position rules API proxy", { timeout: 20_000 }, () => {
  fastApiIt("GET /api/position-rules returns rows", async () => {
    const mod = await import("../app/api/position-rules/route");
    const res = await mod.GET();
    expect(res.status, await res.clone().text()).toBe(200);
    expect(Array.isArray(await res.json())).toBe(true);
  });

  fastApiIt("GET /api/position-rules/health includes counts", async () => {
    const mod = await import("../app/api/position-rules/health/route");
    const res = await mod.GET();
    expect(res.status, await res.clone().text()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty("market_window");
    expect(body).toHaveProperty("rule_counts_by_state");
    expect(body).toHaveProperty("claim_counts_by_status");
  });

  fastApiIt("POST /api/position-rules/[id]/cancel preserves 404", async () => {
    const mod = await import("../app/api/position-rules/[id]/cancel/route");
    const req = new Request("http://localhost/api/position-rules/999999999/cancel", {
      method: "POST",
    });
    const res = await mod.POST(req, { params: Promise.resolve({ id: "999999999" }) });
    expect(res.status, await res.clone().text()).toBe(404);
  });
});
