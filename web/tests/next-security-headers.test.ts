import { afterEach, beforeEach, describe, expect, it } from "vitest";

describe("next.config.mjs security headers", () => {
  const saved = { VERCEL: process.env.VERCEL, RADON_ENABLE_HSTS: process.env.RADON_ENABLE_HSTS };

  beforeEach(() => {
    delete process.env.VERCEL;
    delete process.env.RADON_ENABLE_HSTS;
  });

  afterEach(() => {
    process.env.VERCEL = saved.VERCEL;
    process.env.RADON_ENABLE_HSTS = saved.RADON_ENABLE_HSTS;
  });

  it("applies baseline headers to all paths", async () => {
    const { default: config } = await import("../next.config.mjs");
    expect(typeof config.headers).toBe("function");
    const rows = await config.headers();
    expect(rows).toEqual([
      {
        source: "/:path*",
        headers: expect.any(Array),
      },
    ]);
    const headers = rows[0].headers as { key: string; value: string }[];
    const map = Object.fromEntries(headers.map((h) => [h.key, h.value]));
    expect(map["X-Frame-Options"]).toBe("DENY");
    expect(map["X-Content-Type-Options"]).toBe("nosniff");
    expect(map["Referrer-Policy"]).toBe("strict-origin-when-cross-origin");
    expect(map["Permissions-Policy"]).toContain("camera=()");
    expect(map["Strict-Transport-Security"]).toBeUndefined();
  });

  it("adds HSTS when VERCEL=1", async () => {
    process.env.VERCEL = "1";
    const { default: config } = await import("../next.config.mjs");
    const rows = await config.headers();
    const headers = rows[0].headers as { key: string; value: string }[];
    const hsts = headers.find((h) => h.key === "Strict-Transport-Security");
    expect(hsts?.value).toContain("max-age=31536000");
  });

  it("adds HSTS when RADON_ENABLE_HSTS=1", async () => {
    process.env.RADON_ENABLE_HSTS = "1";
    const { default: config } = await import("../next.config.mjs");
    const rows = await config.headers();
    const headers = rows[0].headers as { key: string; value: string }[];
    expect(headers.some((h) => h.key === "Strict-Transport-Security")).toBe(true);
  });
});
