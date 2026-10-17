/**
 * @vitest-environment jsdom
 */

// Regression: useSyncHook's default POST sync must carry
// Content-Type: application/json — the /api middleware rejects mutating
// requests without it (403 content_type), which broke the Historical Trades
// / blotter sync button. Feeds the hook's ACTUAL request shape through the
// real middleware function rather than asserting the header in isolation.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { NextRequest } from "next/server";
import { useSyncHook } from "../lib/useSyncHook";
import middleware from "../middleware";

type CapturedCall = { url: string; init: RequestInit };

function runThroughMiddleware(call: CapturedCall): ReturnType<typeof middleware> {
  // Browsers add Host/Origin automatically; supply the allowed values and
  // forward the hook's own headers (the Content-Type under test).
  const headers = new Headers(call.init.headers as HeadersInit);
  headers.set("host", "localhost:3000");
  headers.set("origin", "http://localhost:3000");
  const req = new NextRequest(new URL(`http://localhost:3000${call.url}`), {
    method: call.init.method,
    headers,
  });
  return middleware(req);
}

describe("useSyncHook request shape vs /api middleware", () => {
  let calls: CapturedCall[];

  beforeEach(() => {
    calls = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit = {}) => {
        calls.push({ url, init });
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("auto-sync POST passes the real middleware", async () => {
    renderHook(() =>
      useSyncHook({ endpoint: "/api/blotter" }, /* active */ true),
    );

    // Mount fires an initial GET read, then the auto-sync POST.
    const post = await waitFor(() => {
      const found = calls.find((c) => c.init.method === "POST");
      if (!found) throw new Error("POST sync not issued yet");
      return found;
    });

    expect(post.url).toBe("/api/blotter");
    const headers = new Headers(post.init.headers as HeadersInit);
    expect(headers.get("content-type")).toBe("application/json");

    // The exact request shape the hook emits must survive the middleware.
    const res = runThroughMiddleware(post);
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });

  it("GET initial read carries no Content-Type and passes the middleware", async () => {
    renderHook(() => useSyncHook({ endpoint: "/api/blotter" }, true));

    const get = await waitFor(() => {
      const found = calls.find((c) => (c.init.method ?? "GET") === "GET");
      if (!found) throw new Error("initial GET not issued yet");
      return found;
    });

    const headers = new Headers(get.init.headers as HeadersInit);
    expect(headers.get("content-type")).toBeNull();
    const res = runThroughMiddleware(get);
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });
});
