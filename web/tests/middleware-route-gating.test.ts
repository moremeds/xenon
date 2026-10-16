import { describe, it, expect } from "vitest";
import { NextRequest } from "next/server";
import middleware, {
  DEFAULT_ALLOWED_HOSTS,
  MULTIPART_API_PATHS,
  allowedHosts,
} from "../middleware";

// The middleware is a browser-origin/CSRF guard on mutating /api/** requests.
// It must reject before any upstream (FastAPI) call is possible — the default
// export returns a 403 NextResponse directly rather than NextResponse.next().

const req = (
  path: string,
  opts: { method?: string; headers?: Record<string, string> } = {},
): NextRequest =>
  new NextRequest(new URL(`http://localhost:3000${path}`), {
    method: opts.method ?? "POST",
    headers: opts.headers,
  });

const GOOD: Record<string, string> = {
  host: "localhost:3000",
  origin: "http://localhost:3000",
  "content-type": "application/json",
};

const run = (request: NextRequest) => middleware(request);

const statusOf = (request: NextRequest): number => {
  const res = run(request);
  // NextResponse.next() has no meaningful status for the guard check; a
  // rejection is a real 403 response. Inspect x-middleware-next to tell apart.
  const isNext = res.headers.get("x-middleware-next") === "1";
  return isNext ? 200 : res.status;
};

describe("middleware browser-origin guard", () => {
  it("defaults allowed hosts to the localhost variants", () => {
    expect(DEFAULT_ALLOWED_HOSTS).toBe(
      "localhost:3000,127.0.0.1:3000,localhost:3200,127.0.0.1:3200",
    );
    expect(allowedHosts(undefined).has("127.0.0.1:3200")).toBe(true);
  });

  it("lets GET and HEAD with an allowed Host through without Origin/JSON", () => {
    const host = { host: "localhost:3000" };
    expect(
      statusOf(req("/api/portfolio", { method: "GET", headers: host })),
    ).toBe(200);
    expect(
      statusOf(req("/api/portfolio", { method: "HEAD", headers: host })),
    ).toBe(200);
  });

  it("403s foreign-Host GET and HEAD (DNS rebinding read)", async () => {
    for (const method of ["GET", "HEAD"] as const) {
      const res = run(
        req("/api/portfolio", {
          method,
          headers: { host: "victim.attacker.example" },
        }),
      );
      expect(res.status).toBe(403);
      if (method === "GET") {
        expect((await res.json()).reason).toBe("host_not_allowed");
      }
    }
  });

  it("lets non-/api requests through unguarded (any Host)", () => {
    expect(statusOf(req("/portfolio", { method: "POST" }))).toBe(200);
    expect(
      statusOf(
        req("/portfolio", {
          method: "GET",
          headers: { host: "victim.attacker.example" },
        }),
      ),
    ).toBe(200);
  });

  it("passes a legitimate same-origin JSON POST", () => {
    const res = run(req("/api/orders", { headers: GOOD }));
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });

  it("403s a cross-origin text/plain POST (CSRF simple request)", async () => {
    const res = run(
      req("/api/orders", {
        headers: {
          host: "localhost:3000",
          origin: "https://evil.example",
          "content-type": "text/plain",
        },
      }),
    );
    expect(res.status).toBe(403);
    expect((await res.json()).reason).toBe("origin_mismatch");
  });

  it("403s a foreign Host (DNS rebinding)", async () => {
    const res = run(
      req("/api/orders", {
        headers: { ...GOOD, host: "victim.attacker.example" },
      }),
    );
    expect(res.status).toBe(403);
    expect((await res.json()).reason).toBe("host_not_allowed");
  });

  it("403s missing Origin without Sec-Fetch-Site: same-origin", async () => {
    const res = run(
      req("/api/orders", {
        headers: {
          host: "localhost:3000",
          "content-type": "application/json",
        },
      }),
    );
    expect(res.status).toBe(403);
    expect((await res.json()).reason).toBe("missing_origin");
  });

  it("passes missing Origin when Sec-Fetch-Site is same-origin", () => {
    const res = run(
      req("/api/orders", {
        headers: {
          host: "localhost:3000",
          "sec-fetch-site": "same-origin",
          "content-type": "application/json",
        },
      }),
    );
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });

  it("403s same-origin host but non-JSON content type", async () => {
    const res = run(
      req("/api/orders", {
        headers: { ...GOOD, "content-type": "text/plain" },
      }),
    );
    expect(res.status).toBe(403);
    expect((await res.json()).reason).toBe("content_type");
  });

  it("403s multipart on routes not in MULTIPART_API_PATHS", async () => {
    expect(MULTIPART_API_PATHS.size).toBe(0);
    const res = run(
      req("/api/orders", {
        headers: { ...GOOD, "content-type": "multipart/form-data; boundary=x" },
      }),
    );
    expect(res.status).toBe(403);
    expect((await res.json()).reason).toBe("content_type");
  });

  it("returns a direct 403 response (zero upstream fetch possible)", () => {
    // A rejected request never yields NextResponse.next() — the middleware
    // short-circuits, so no route handler or FastAPI call can run.
    const res = run(req("/api/orders", { headers: { host: "evil.example" } }));
    expect(res.status).toBe(403);
    expect(res.headers.get("x-middleware-next")).toBeNull();
  });
});
