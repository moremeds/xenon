/**
 * The direct web→api callers must attach the internal credential.
 *
 * previous-close mints WS tickets through xenonFetch; wizard/stream proxies an
 * SSE response through the shared internalApiHeaders helper (xenonFetch would
 * try to res.json() a stream). Both must send X-Internal-Token — without it
 * FastAPI fail-closed auth returns 401.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("ws", () => {
  class MockWebSocket {
    static constructedUrls: string[] = [];
    private handlers = new Map<string, ((...a: unknown[]) => void)[]>();
    constructor(url: string) {
      MockWebSocket.constructedUrls.push(url);
      queueMicrotask(() => {
        this.handlers.get("error")?.forEach((f) => f(new Error("mock ws")));
      });
    }
    on(event: string, fn: (...a: unknown[]) => void) {
      const list = this.handlers.get(event) ?? [];
      list.push(fn);
      this.handlers.set(event, list);
      return this;
    }
    send() {}
    close() {}
  }
  return { WebSocket: MockWebSocket };
});

type CapturedFetch = { url: string; headers: Headers };

describe("internal credential on direct web→api callers", () => {
  let calls: CapturedFetch[];

  beforeEach(() => {
    calls = [];
    process.env.XENON_INTERNAL_API_TOKEN = "itok-test-123";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string | URL, init: RequestInit = {}) => {
        const headers = new Headers(init.headers as HeadersInit);
        calls.push({ url: String(url), headers });
        if (String(url).includes("/ws-ticket")) {
          return new Response(JSON.stringify({ ticket: "tk-1" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response("upstream", { status: 200 });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.XENON_INTERNAL_API_TOKEN;
  });

  it("previous-close mints /ws-ticket with X-Internal-Token", async () => {
    const { POST } = await import("../app/api/previous-close/route");
    const { WebSocket } = await import("ws");
    const res = await POST(
      new Request("http://localhost:3000/api/previous-close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: ["ILF"] }),
      }),
    );
    expect(res.status).toBe(200);

    const ticketCall = calls.find((c) => c.url.includes("/ws-ticket"));
    expect(ticketCall).toBeDefined();
    expect(ticketCall!.headers.get("x-internal-token")).toBe("itok-test-123");

    // The minted ticket reaches the WS upgrade URL (relay requires it).
    const wsUrls = (WebSocket as unknown as { constructedUrls: string[] })
      .constructedUrls;
    expect(wsUrls[0]).toContain("ticket=tk-1");
  });

  it("wizard/stream proxies SSE with X-Internal-Token", async () => {
    const { GET } = await import("../app/api/wizard/stream/route");
    const res = await GET(
      new Request("http://localhost:3000/api/wizard/stream?session_id=s1"),
    );
    expect(res.status).toBe(200);

    const upstream = calls.find((c) => c.url.includes("/wizard/stream"));
    expect(upstream).toBeDefined();
    expect(upstream!.headers.get("x-internal-token")).toBe("itok-test-123");
    expect(upstream!.headers.get("accept")).toBe("text/event-stream");
  });
});
