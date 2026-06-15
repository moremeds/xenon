import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";

// Clerk's auth() is mocked so the route's gate can be driven per-test. vi.mock
// is hoisted, so the spy is created via vi.hoisted to be referenceable inside
// the factory.
const { authMock } = vi.hoisted(() => ({ authMock: vi.fn() }));
vi.mock("@clerk/nextjs/server", () => ({ auth: authMock }));

import { GET, parseUwQuotaHeaders } from "@/app/api/admin/uw-quota/route";

beforeEach(() => {
  // Default: an authenticated session.
  authMock.mockResolvedValue({ userId: "user_test" });
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.clearAllMocks();
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
  it("returns 401 when there is no Clerk session", async () => {
    authMock.mockResolvedValue({ userId: null });
    const res = await GET();
    expect(res.status).toBe(401);
    const body = await res.json();
    expect(body.error).toBe("Unauthorized");
  });

  it("bypasses auth when XENON_DISABLE_AUTH=1 (dev/E2E)", async () => {
    authMock.mockResolvedValue({ userId: null });
    vi.stubEnv("XENON_DISABLE_AUTH", "1");
    vi.stubEnv("UW_TOKEN", "");
    const res = await GET();
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.configured).toBe(false);
  });

  it("returns configured:false when UW_TOKEN is unset (authed)", async () => {
    vi.stubEnv("UW_TOKEN", "");
    const res = await GET();
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.configured).toBe(false);
    expect(body.daily_count).toBeNull();
  });
});
