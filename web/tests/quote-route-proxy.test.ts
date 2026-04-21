import { describe, test, expect, vi } from "vitest";

vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: vi.fn(async (path: string) => {
    expect(path).toMatch(/^\/orders\/quote\?ticker=SPY&con_id=756733$/);
    return new Response(
      JSON.stringify({
        token: "stub.token",
        bid: "500.10",
        ask: "500.20",
        bid_size: 100,
        ask_size: 120,
        ts_server_ms: 1,
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }),
}));

import { GET } from "../app/api/orders/quote/route";

describe("/api/orders/quote", () => {
  test("forwards to FastAPI and returns token payload", async () => {
    const req = new Request(
      "http://localhost/api/orders/quote?ticker=SPY&con_id=756733",
    );
    const res = await GET(req);
    const body = await res.json();
    expect(res.status).toBe(200);
    expect(body.token).toBe("stub.token");
  });
});
