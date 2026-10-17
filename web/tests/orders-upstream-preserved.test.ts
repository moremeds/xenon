/**
 * F6.3 — Verify /api/orders/{place,cancel,modify} preserve upstream
 * status + JSON body verbatim, and fall back to 500 + request_id only
 * for unexpected errors.
 */
import { describe, test, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("@/lib/xenonApi", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/xenonApi")>("@/lib/xenonApi");
  return {
    ...actual,
    xenonFetch: vi.fn(),
  };
});

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
      client_attempt_id: "test-orders-upstream",
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
  afterEach(() => {
    // Tests that 400 before reaching xenonFetch leave queued Once-mocks
    // unconsumed; drain them so they can't leak into the next test.
    mockFetch.mockReset();
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

  test("preserves top-level reason_code on cancel (PR-05 contract)", async () => {
    // New FastAPI shape: reason_code at the TOP level for the toast helpers,
    // structured payload still under detail for existing readers.
    const upstreamBody = {
      reason_code: "IB_CONNECTION",
      detail: {
        reason_code: "IB_CONNECTION",
        classification: "connection",
        message: "IB gateway unreachable",
        http_status: 503,
      },
    };
    mockFetch.mockRejectedValueOnce(
      new XenonApiError(503, "IB_CONNECTION", upstreamBody),
    );
    const res = await cancelPost(cancelReq());
    expect(res.status).toBe(503);
    const body = await res.json();
    expect(body).toEqual(upstreamBody);
    expect(body.reason_code).toBe("IB_CONNECTION");
  });

  test("preserves top-level reason_code + detail.applied on modify (PR-05)", async () => {
    const upstreamBody = {
      reason_code: "MODIFY_STALE",
      detail: {
        reason_code: "MODIFY_STALE",
        message: "modifySequence 3 is stale; current is 5",
        applied: 5,
        http_status: 409,
      },
    };
    mockFetch.mockRejectedValueOnce(
      new XenonApiError(409, "MODIFY_STALE", upstreamBody),
    );
    const res = await modifyPost(modifyReq());
    expect(res.status).toBe(409);
    const body = await res.json();
    expect(body).toEqual(upstreamBody);
    expect(body.reason_code).toBe("MODIFY_STALE");
    expect(body.detail.applied).toBe(5);
  });

  test("preserves 409 OWNERSHIP identity rejection on cancel (PR-05)", async () => {
    const upstreamBody = {
      reason_code: "OWNERSHIP",
      detail: {
        reason_code: "OWNERSHIP",
        classification: "ownership",
        message:
          "Order identity mismatch — refusing to act: 2 open orders share orderId=10",
        http_status: 409,
      },
    };
    mockFetch.mockRejectedValueOnce(
      new XenonApiError(409, "OWNERSHIP", upstreamBody),
    );
    const res = await cancelPost(cancelReq());
    expect(res.status).toBe(409);
    const body = await res.json();
    expect(body).toEqual(upstreamBody);
    expect(body.reason_code).toBe("OWNERSHIP");
  });

  test("local cancel validation carries ORDER_IDENTIFIER_REQUIRED", async () => {
    const req = new Request("http://localhost/api/orders/cancel", {
      method: "POST",
      body: JSON.stringify({ orderId: 0, permId: 0 }),
    });
    const res = await cancelPost(req);
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.reason_code).toBe("ORDER_IDENTIFIER_REQUIRED");
  });

  test("local modify validation carries reason_code", async () => {
    const req = new Request("http://localhost/api/orders/modify", {
      method: "POST",
      body: JSON.stringify({ orderId: 0, permId: 0, newPrice: 5.5 }),
    });
    const res = await modifyPost(req);
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.reason_code).toBe("ORDER_IDENTIFIER_REQUIRED");

    const zeroPrice = new Request("http://localhost/api/orders/modify", {
      method: "POST",
      body: JSON.stringify({ orderId: 1, newPrice: 0 }),
    });
    const res2 = await modifyPost(zeroPrice);
    expect(res2.status).toBe(400);
    expect((await res2.json()).reason_code).toBe("INVALID_ORDER_BODY");
  });

  test("negative newPrice passes through — BAG combos carry signed limits", async () => {
    // Credit combos carry a NEGATIVE BAG limit. The route has no combo flag,
    // so it must NOT reject negatives locally — the CLI knows the located
    // trade's secType and decides. Only 0 is an unconditional local 400.
    mockFetch.mockResolvedValueOnce({ status: "ok", message: "Modified" });
    mockFetch.mockResolvedValueOnce({ open_orders: [] }); // /orders/refresh
    mockFetch.mockResolvedValueOnce({
      open_orders: [
        {
          orderId: 1,
          permId: 0,
          limitPrice: -1.25,
          totalQuantity: 1,
        },
      ],
    }); // refreshed orders for confirmation

    const req = new Request("http://localhost/api/orders/modify", {
      method: "POST",
      body: JSON.stringify({ orderId: 1, newPrice: -1.25 }),
    });
    const res = await modifyPost(req);
    expect(res.status).toBe(200);
    const [path, init] = mockFetch.mock.calls[0];
    expect(path).toBe("/orders/modify");
    expect(JSON.parse(init.body as string).newPrice).toBe(-1.25);
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
