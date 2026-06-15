import { describe, it, expect, vi, afterEach } from "vitest";

import { GET, parseUwQuotaHeaders } from "@/app/api/admin/uw-quota/route";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("parseUwQuotaHeaders", () => {
  it("parses the x-uw-* rate-limit headers", () => {
    const h = new Headers({
      "x-uw-daily-req-count": "1234",
      "x-uw-token-req-limit": "100000",
      "x-uw-minute-req-counter": "3",
      "x-uw-req-per-minute-remaining": "57",
      "x-uw-req-per-minute-reset": "42000",
    });
    const q = parseUwQuotaHeaders(h, "2026-06-15T14:00:00Z");
    expect(q.configured).toBe(true);
    expect(q.daily_count).toBe(1234);
    expect(q.daily_limit).toBe(100000);
    expect(q.minute_count).toBe(3);
    expect(q.minute_remaining).toBe(57);
    expect(q.minute_reset_ms).toBe(42000);
  });

  it("returns null for missing/blank headers", () => {
    const q = parseUwQuotaHeaders(new Headers({}), "2026-06-15T14:00:00Z");
    expect(q.daily_count).toBeNull();
    expect(q.daily_limit).toBeNull();
    expect(q.minute_remaining).toBeNull();
  });
});

describe("GET /api/admin/uw-quota", () => {
  it("returns configured:false when UW_TOKEN is unset", async () => {
    vi.stubEnv("UW_TOKEN", "");
    const res = await GET();
    const body = await res.json();
    expect(body.configured).toBe(false);
    expect(body.daily_count).toBeNull();
  });
});
