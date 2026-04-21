import { describe, test, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("@/lib/xenonApi", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/xenonApi")>("@/lib/xenonApi");
  return {
    ...actual,
    xenonFetch: vi.fn(async (_path: string, init: RequestInit | undefined) => {
      const body = JSON.parse((init?.body as string) ?? "{}");
      expect(body.client_attempt_id).toBe("c-1");
      expect(body.quote_token).toBe("t.sig");
      throw new actual.XenonApiError(409, "terminal", {
        detail: "terminal",
        reason_code: "ATTEMPT_ID_TERMINAL",
      });
    }),
  };
});

vi.mock("@tools/data-reader", () => ({
  readDataFile: vi.fn(async () => ({ ok: true, data: { positions: [] } })),
}));

import { POST } from "../app/api/orders/place/route";

describe("/api/orders/place — idempotency passthrough", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  test("forwards client_attempt_id + quote_token and preserves 409", async () => {
    const req = new Request("http://localhost/api/orders/place", {
      method: "POST",
      body: JSON.stringify({
        type: "stock",
        symbol: "SPY",
        action: "BUY",
        quantity: 1,
        limitPrice: 500,
        client_attempt_id: "c-1",
        quote_token: "t.sig",
      }),
    });
    const res = await POST(req);
    expect(res.status).toBe(409);
    const j = await res.json();
    expect(j.reason_code).toBe("ATTEMPT_ID_TERMINAL");
  });
});
