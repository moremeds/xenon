/**
 * PR-05 round 7 — xenonFetch must preserve non-JSON upstream bodies.
 *
 * Regression for the consumed-body bug: xenonFetch called res.json() then
 * res.text() in the catch — the body was already consumed, so a non-JSON
 * upstream error (e.g. nginx/gateway 502 text) collapsed to "HTTP <status>"
 * and the human-readable detail never reached the route or the toast.
 *
 * These tests run the REAL xenonFetch against a mocked global fetch that
 * returns real Response objects, through the real cancel/modify Next
 * routes, asserting the passthrough payload keeps the upstream text.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { POST as cancelPost } from "@/app/api/orders/cancel/route";
import { POST as modifyPost } from "@/app/api/orders/modify/route";

function makeRequest(url: string, body: unknown): Request {
  return new Request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function stubUpstream(responses: Array<{ status: number; body: string }>) {
  // Fresh Response per call — bodies are single-use streams.
  const spy = vi.spyOn(globalThis, "fetch");
  for (const r of responses) {
    spy.mockImplementationOnce(async () => new Response(r.body, { status: r.status }));
  }
  return spy;
}

describe("xenonFetch detail preservation through the Next routes", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("cancel: non-JSON upstream body reaches the client as {detail}", async () => {
    stubUpstream([{ status: 502, body: "upstream unavailable" }]);
    const res = await cancelPost(
      makeRequest("http://localhost/api/orders/cancel", {
        orderId: 10,
        permId: 12345,
      }),
    );
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ detail: "upstream unavailable" });
  });

  it("cancel: empty upstream body falls back to HTTP <status>", async () => {
    stubUpstream([{ status: 502, body: "" }]);
    const res = await cancelPost(
      makeRequest("http://localhost/api/orders/cancel", {
        orderId: 10,
        permId: 12345,
      }),
    );
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ detail: "HTTP 502" });
  });

  it("modify: non-JSON upstream body reaches the client as {detail}", async () => {
    stubUpstream([{ status: 502, body: "upstream unavailable" }]);
    const res = await modifyPost(
      makeRequest("http://localhost/api/orders/modify", {
        orderId: 10,
        permId: 12345,
        newPrice: 22.5,
      }),
    );
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ detail: "upstream unavailable" });
  });

  it("modify: empty upstream body falls back to HTTP <status>", async () => {
    stubUpstream([{ status: 502, body: "" }]);
    const res = await modifyPost(
      makeRequest("http://localhost/api/orders/modify", {
        orderId: 10,
        permId: 12345,
        newPrice: 22.5,
      }),
    );
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ detail: "HTTP 502" });
  });
});
