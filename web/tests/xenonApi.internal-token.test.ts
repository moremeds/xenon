import { describe, it, expect, vi, afterEach } from "vitest";
import { xenonFetch, internalApiHeaders } from "../lib/xenonApi";

function jsonResponse() {
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

describe("internalApiHeaders", () => {
  afterEach(() => {
    delete process.env.XENON_INTERNAL_API_TOKEN;
  });

  it("sets X-Internal-Token when env present", () => {
    process.env.XENON_INTERNAL_API_TOKEN = "s3cret";
    const h = new Headers();
    internalApiHeaders(h);
    expect(h.get("X-Internal-Token")).toBe("s3cret");
  });

  it("no-op when env absent", () => {
    delete process.env.XENON_INTERNAL_API_TOKEN;
    const h = new Headers();
    internalApiHeaders(h);
    expect(h.get("X-Internal-Token")).toBeNull();
  });
});

describe("xenonFetch internal token", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.XENON_INTERNAL_API_TOKEN;
  });

  it("attaches X-Internal-Token when env set", async () => {
    process.env.XENON_INTERNAL_API_TOKEN = "s3cret";
    const fetchMock = vi.fn(async () => jsonResponse());
    vi.stubGlobal("fetch", fetchMock);
    await xenonFetch("/portfolio");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("X-Internal-Token")).toBe("s3cret");
  });

  it("omits X-Internal-Token when env unset", async () => {
    delete process.env.XENON_INTERNAL_API_TOKEN;
    const fetchMock = vi.fn(async () => jsonResponse());
    vi.stubGlobal("fetch", fetchMock);
    await xenonFetch("/portfolio");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("X-Internal-Token")).toBeNull();
  });
});
