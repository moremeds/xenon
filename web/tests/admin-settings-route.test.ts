import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/server", () => ({
  NextResponse: {
    json: (body: unknown, init?: ResponseInit) =>
      new Response(JSON.stringify(body), {
        ...init,
        headers: { "content-type": "application/json" },
      }),
  },
}));

const mocks = vi.hoisted(() => ({ xenonFetch: vi.fn() }));

vi.mock("@/lib/xenonApi", () => {
  class XenonApiError extends Error {
    constructor(
      public readonly status: number,
      public readonly detail: string,
      public readonly body: Record<string, unknown> | null = null,
    ) {
      super(detail);
    }
  }
  return { xenonFetch: mocks.xenonFetch, XenonApiError };
});

const params = (key: string) => ({ params: Promise.resolve({ key }) });

describe("/api/admin/settings routes", () => {
  beforeEach(() => {
    vi.resetModules();
    mocks.xenonFetch.mockReset();
  });

  it("GET proxies /admin/settings through xenonFetch", async () => {
    mocks.xenonFetch.mockResolvedValue({ settings: [] });
    const { GET } = await import("@/app/api/admin/settings/route");
    const res = await GET();
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ settings: [] });
    expect(mocks.xenonFetch).toHaveBeenCalledWith("/admin/settings", expect.objectContaining({ method: "GET" }));
  });

  it("PUT forwards value, note and confirm", async () => {
    mocks.xenonFetch.mockResolvedValue({ key: "orders_paused", changed: true });
    const { PUT } = await import("@/app/api/admin/settings/[key]/route");
    const req = new Request("http://x/api/admin/settings/orders_paused", {
      method: "PUT",
      body: JSON.stringify({ value: true, note: "halt", confirm: "orders_paused", extra: 1 }),
    });
    const res = await PUT(req, params("orders_paused"));
    expect(res.status).toBe(200);
    const [path, init] = mocks.xenonFetch.mock.calls[0];
    expect(path).toBe("/admin/settings/orders_paused");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({ value: true, note: "halt", confirm: "orders_paused" });
  });

  it("DELETE forwards note and confirm as query", async () => {
    mocks.xenonFetch.mockResolvedValue({ key: "XENON_FUTU_TRADING", changed: true });
    const { DELETE } = await import("@/app/api/admin/settings/[key]/route");
    const req = new Request(
      "http://x/api/admin/settings/XENON_FUTU_TRADING?note=revert&confirm=XENON_FUTU_TRADING&x=1",
      { method: "DELETE" },
    );
    await DELETE(req, params("XENON_FUTU_TRADING"));
    const [path, init] = mocks.xenonFetch.mock.calls[0];
    expect(init.method).toBe("DELETE");
    expect(path).toBe("/admin/settings/XENON_FUTU_TRADING?note=revert&confirm=XENON_FUTU_TRADING");
  });

  it("passes upstream status and top-level reason_code through", async () => {
    const { XenonApiError } = await import("@/lib/xenonApi");
    mocks.xenonFetch.mockRejectedValue(
      new XenonApiError(428, "needs confirm", { reason_code: "SETTING_CONFIRM_REQUIRED", detail: "needs confirm" }),
    );
    const { PUT } = await import("@/app/api/admin/settings/[key]/route");
    const req = new Request("http://x", { method: "PUT", body: JSON.stringify({ value: true }) });
    const res = await PUT(req, params("XENON_AUDIT_CANCEL"));
    expect(res.status).toBe(428);
    expect((await res.json()).reason_code).toBe("SETTING_CONFIRM_REQUIRED");
  });

  it("refuses a malformed key or body without calling the api", async () => {
    const { PUT, DELETE } = await import("@/app/api/admin/settings/[key]/route");
    const bad = await PUT(new Request("http://x", { method: "PUT", body: "{}" }), params("../orders"));
    expect(bad.status).toBe(400);
    expect((await bad.json()).reason_code).toBe("SETTING_UNKNOWN");
    const noValue = await PUT(new Request("http://x", { method: "PUT", body: "{}" }), params("orders_paused"));
    expect(noValue.status).toBe(400);
    expect((await noValue.json()).reason_code).toBe("SETTING_INVALID_VALUE");
    const badDel = await DELETE(new Request("http://x", { method: "DELETE" }), params("a/b"));
    expect(badDel.status).toBe(400);
    expect(mocks.xenonFetch).not.toHaveBeenCalled();
  });
});
