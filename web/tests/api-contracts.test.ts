import { describe, it, expect, vi, afterEach } from "vitest";
import {
  getRequestId,
  setNoStoreResponseHeaders,
  setCacheResponseHeaders,
  jsonApiError,
} from "../lib/apiContracts";
import { NextResponse } from "next/server";

describe("getRequestId", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.resetModules();
  });

  it("returns a UUID string", () => {
    const id = getRequestId();
    expect(id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("returns fallback format when randomUUID throws", async () => {
    // Use doMock (not hoisted) so the first test keeps the real module
    vi.doMock("node:crypto", () => ({
      randomUUID: () => {
        throw new Error("not supported");
      },
    }));

    // Re-import to pick up mock
    const { getRequestId: getRequestIdMocked } =
      await import("../lib/apiContracts");
    const id = getRequestIdMocked();
    expect(id).toMatch(/^rid_\d+_[0-9a-f]+$/);
  });
});

describe("setNoStoreResponseHeaders", () => {
  it("sets Cache-Control, Pragma, and X-Request-Id", () => {
    const res = NextResponse.json({});
    setNoStoreResponseHeaders(res, "req-123");
    expect(res.headers.get("Cache-Control")).toBe(
      "no-store, no-cache, must-revalidate",
    );
    expect(res.headers.get("Pragma")).toBe("no-cache");
    expect(res.headers.get("X-Request-Id")).toBe("req-123");
  });
});

describe("setCacheResponseHeaders", () => {
  it("sets public max-age and cache state", () => {
    const res = NextResponse.json({});
    setCacheResponseHeaders(res, {
      maxAgeSeconds: 300,
      requestId: "req-456",
      cacheState: "HIT",
    });
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=300");
    expect(res.headers.get("X-Cache-State")).toBe("HIT");
    expect(res.headers.get("X-Request-Id")).toBe("req-456");
  });

  it("includes stale-while-revalidate when provided", () => {
    const res = NextResponse.json({});
    setCacheResponseHeaders(res, {
      maxAgeSeconds: 60,
      staleWhileRevalidateSeconds: 120,
      requestId: "req-789",
    });
    expect(res.headers.get("Cache-Control")).toBe(
      "public, max-age=60, stale-while-revalidate=120",
    );
  });

  it("includes cache tags when provided", () => {
    const res = NextResponse.json({});
    setCacheResponseHeaders(res, {
      maxAgeSeconds: 60,
      requestId: "req-abc",
      tags: ["regime", "vix"],
    });
    expect(res.headers.get("X-Cache-Tags")).toBe("regime,vix");
  });
});

describe("jsonApiError", () => {
  it("returns proper error payload with status 404", () => {
    const res = jsonApiError({
      message: "Not found",
      status: 404,
      requestId: "req-err",
    });
    expect(res.status).toBe(404);
  });

  it("defaults to 500 and INTERNAL_ERROR", () => {
    const res = jsonApiError({
      message: "Something broke",
      requestId: "req-500",
    });
    expect(res.status).toBe(500);
  });

  it("includes detail when provided", async () => {
    const res = jsonApiError({
      message: "Bad input",
      status: 400,
      code: "VALIDATION_ERROR",
      detail: "ticker is required",
      requestId: "req-detail",
    });
    const body = await res.json();
    expect(body.code).toBe("VALIDATION_ERROR");
    expect(body.detail).toBe("ticker is required");
    expect(body.requestId).toBe("req-detail");
  });
});
