import { describe, test, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/xenonApi", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/xenonApi")>("@/lib/xenonApi");
  return {
    ...actual,
    xenonFetch: vi.fn(async () => ({ orderId: "O1" })),
  };
});

vi.mock("@tools/data-reader", () => ({
  readDataFile: vi.fn(async () => ({ ok: true, data: { positions: [] } })),
}));

import { POST } from "../app/api/orders/place/route";
import * as xenonApi from "@/lib/xenonApi";

describe("/api/orders/place — quote_tokens passthrough", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test("forwards quote_tokens and per-leg con_id to FastAPI", async () => {
    const body = {
      type: "combo",
      symbol: "SPY",
      action: "SELL",
      quantity: 1,
      limitPrice: 2.3,
      tif: "DAY",
      legs: [
        {
          conId: 111,
          expiry: "2026-05-16",
          strike: 500,
          right: "C",
          action: "BUY",
          ratio: 1,
        },
        {
          conId: 222,
          expiry: "2026-05-16",
          strike: 510,
          right: "C",
          action: "SELL",
          ratio: 1,
        },
      ],
      quote_tokens: { "111": "tok-111", "222": "tok-222" },
      client_attempt_id: "attempt-1",
    };
    const req = new Request("http://localhost/api/orders/place", {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Content-Type": "application/json" },
    });
    await POST(req);

    expect(xenonApi.xenonFetch).toHaveBeenCalled();
    const forwarded = (
      xenonApi.xenonFetch as unknown as { mock: { calls: unknown[][] } }
    ).mock.calls[0][1] as { body: string };
    const forwardedBody = JSON.parse(forwarded.body);
    expect(forwardedBody.quote_tokens).toEqual({
      "111": "tok-111",
      "222": "tok-222",
    });
    expect(forwardedBody.legs[0].con_id).toBe(111);
    expect(forwardedBody.legs[1].con_id).toBe(222);
  });
});
