/**
 * F6.3 — Verify /api/orders/{place,cancel,modify} preserve upstream
 * status + JSON body verbatim, and fall back to 500 + request_id only
 * for unexpected errors.
 */
import { describe, test, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/xenonApi", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/xenonApi")>("@/lib/xenonApi");
  return {
    ...actual,
    xenonFetch: vi.fn(),
  };
});

vi.mock("@tools/data-reader", () => ({
  readDataFile: vi.fn(async () => ({ ok: true, data: { positions: [] } })),
}));

import { xenonFetch, XenonApiError } from "@/lib/xenonApi";
import { POST as placePost } from "../app/api/orders/place/route";
import { POST as cancelPost } from "../app/api/orders/cancel/route";
import { POST as modifyPost } from "../app/api/orders/modify/route";

const mockFetch = xenonFetch as unknown as ReturnType<typeof vi.fn>;

function placeReq(extra: Record<string, unknown> = {}): Request {
  return new Request("http://localhost/api/orders/place", {
    method: "POST",
    body: JSON.stringify({
      type: "stock",
      symbol: "SPY",
      action: "BUY",
      quantity: 1,
      limitPrice: 500,
      ...extra,
    }),
  });
}

function cancelReq(): Request {
  return new Request("http://localhost/api/orders/cancel", {
    method: "POST",
    body: JSON.stringify({ orderId: 123 }),
  });
}

function modifyReq(): Request {
  return new Request("http://localhost/api/orders/modify", {
    method: "POST",
    body: JSON.stringify({ orderId: 123, newPrice: 5.5 }),
  });
}

describe("/api/orders/* — upstream status + detail passthrough", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test("preserves 502 detail verbatim (IB_CONNECTION on place)", async () => {
    const upstreamBody = {
      detail: {
        reason_code: "IB_CONNECTION",
        message: "IB gateway disconnected",
        retryable: true,
      },
    };
    mockFetch.mockRejectedValueOnce(
      new XenonApiError(502, "IB_CONNECTION", upstreamBody),
    );
    const res = await placePost(placeReq());
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual(upstreamBody);
  });

  test("preserves 409 modify_stale with applied count", async () => {
    const upstreamBody = {
      detail: {
        reason_code: "MODIFY_STALE",
        applied: 3,
        requested: 2,
      },
    };
    mockFetch.mockRejectedValueOnce(
      new XenonApiError(409, "MODIFY_STALE", upstreamBody),
    );
    const res = await modifyPost(modifyReq());
    expect(res.status).toBe(409);
    const body = await res.json();
    expect(body).toEqual(upstreamBody);
    expect(body.detail.applied).toBe(3);
  });

  test("preserves 503 IB_CONNECTION verbatim on cancel", async () => {
    const upstreamBody = {
      detail: {
        reason_code: "IB_CONNECTION",
        message: "IB unavailable",
      },
    };
    mockFetch.mockRejectedValueOnce(
      new XenonApiError(503, "IB_CONNECTION", upstreamBody),
    );
    const res = await cancelPost(cancelReq());
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual(upstreamBody);
  });

  test("unknown error falls through to 500 with request_id", async () => {
    mockFetch.mockRejectedValueOnce(new Error("boom — not a XenonApiError"));
    const res = await cancelPost(cancelReq());
    expect(res.status).toBe(500);
    const body = await res.json();
    expect(body.error).toBe("internal");
    expect(typeof body.request_id).toBe("string");
    expect(body.request_id.length).toBeGreaterThan(0);
    // No stack trace leaked
    expect(JSON.stringify(body)).not.toContain("boom");
  });

  test("preserves 409 ATTEMPT_ID_TERMINAL on place (F4 behavior intact)", async () => {
    const upstreamBody = {
      detail: "terminal",
      reason_code: "ATTEMPT_ID_TERMINAL",
    };
    mockFetch.mockRejectedValueOnce(
      new XenonApiError(409, "terminal", upstreamBody),
    );
    const res = await placePost(
      placeReq({ client_attempt_id: "c-1", quote_token: "t.sig" }),
    );
    expect(res.status).toBe(409);
    const body = await res.json();
    expect(body.reason_code).toBe("ATTEMPT_ID_TERMINAL");
  });
});
