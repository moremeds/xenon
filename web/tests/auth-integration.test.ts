import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * Tests for credential forwarding in xenonFetch and wsTicket.
 *
 * Covers:
 * - X-Internal-Token injection when XENON_INTERNAL_API_TOKEN is set
 * - Header omission when the env var is absent
 * - wsTicket API call shape and error handling
 */

describe("xenonFetch internal token", () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.resetModules();
    mockFetch.mockReset();
    globalThis.fetch = mockFetch as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("sends X-Internal-Token when XENON_INTERNAL_API_TOKEN is set", async () => {
    vi.stubEnv("XENON_INTERNAL_API_TOKEN", "internal-secret");
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ data: "test" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const { xenonFetch } = await import("@/lib/xenonApi");
    await xenonFetch("/test");

    const [, options] = mockFetch.mock.calls[0];
    const headers = new Headers(options.headers);
    expect(headers.get("X-Internal-Token")).toBe("internal-secret");
  });

  it("sends no auth header when the env var is absent", async () => {
    vi.stubEnv("XENON_INTERNAL_API_TOKEN", "");
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ data: "test" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const { xenonFetch } = await import("@/lib/xenonApi");
    await xenonFetch("/test");

    const [, options] = mockFetch.mock.calls[0];
    const headers = new Headers(options.headers);
    expect(headers.get("X-Internal-Token")).toBeNull();
    expect(headers.get("Authorization")).toBeNull();
  });
});

describe("wsTicket", () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.resetModules();
    mockFetch.mockReset();
    globalThis.fetch = mockFetch as unknown as typeof fetch;
  });

  it("posts to the same-origin route with JSON content type", async () => {
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ ticket: "uuid-ticket-123" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const { getWsTicket } = await import("@/lib/wsTicket");
    const ticket = await getWsTicket();

    expect(ticket).toBe("uuid-ticket-123");
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/ib/ws-ticket");
    expect(options.method).toBe("POST");
    expect(
      new Headers(options.headers).get("Content-Type"),
    ).toBe("application/json");
  });

  it("throws on non-ok response", async () => {
    mockFetch.mockResolvedValue(new Response("Forbidden", { status: 403 }));

    const { getWsTicket } = await import("@/lib/wsTicket");
    await expect(getWsTicket()).rejects.toThrow(
      "Failed to obtain WS ticket: 403",
    );
  });
});
