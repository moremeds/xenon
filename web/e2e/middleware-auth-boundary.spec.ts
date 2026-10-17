import {
  createServer,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import { expect, test } from "@playwright/test";

// Real-dispatch proof for the /api/** browser-origin guard in
// web/middleware.ts: unit tests call the default export directly, but only a
// live Next dev server proves the /api/:path* matcher rejects before the
// route handler runs. A counting mock FastAPI on 127.0.0.1:8325 (the env
// XENON_API_URL the dev server is launched with) records every upstream hit;
// any middleware-side rejection must leave the count at zero.

const MOCK_BACKEND_PORT = 8325;
const upstreamHits: string[] = [];

let backendServer: Server | null = null;

test.beforeAll(async () => {
  backendServer = createServer(
    (req: IncomingMessage, res: ServerResponse) => {
      upstreamHits.push(`${req.method} ${req.url}`);
      res.statusCode = 200;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ ok: true }));
    },
  );
  await new Promise<void>((resolve, reject) => {
    backendServer?.once("error", reject);
    backendServer?.listen(MOCK_BACKEND_PORT, "127.0.0.1", () => resolve());
  });
});

test.beforeEach(() => {
  upstreamHits.length = 0;
});

test.afterAll(async () => {
  if (!backendServer) return;
  await new Promise<void>((resolve, reject) => {
    backendServer?.close((error) => (error ? reject(error) : resolve()));
  });
  backendServer = null;
});

// The dev server under test is launched with
// XENON_ALLOWED_HOSTS=localhost:<PLAYWRIGHT_PORT> so the default baseURL Host
// is the allowed one; a forged Host exercises the rebinding path.
const ALLOWED_HOST = `localhost:${process.env.PLAYWRIGHT_PORT || 3200}`;

test.describe("middleware auth boundary (real Next dispatch)", () => {
  test("foreign Host -> 403 on every method, zero upstream hits", async ({
    request,
  }) => {
    for (const method of ["GET", "HEAD", "POST", "DELETE"] as const) {
      const res = await request.fetch("/api/blotter", {
        method,
        headers: { host: "victim.attacker.example" },
        data: method === "POST" ? "{}" : undefined,
      });
      expect(res.status()).toBe(403);
    }
    expect(upstreamHits).toEqual([]);
  });

  test("allowed Host + Sec-Fetch-Site cross-site -> 403 on GET/HEAD, zero upstream hits", async ({
    request,
  }) => {
    for (const method of ["GET", "HEAD"] as const) {
      const res = await request.fetch("/api/portfolio", {
        method,
        headers: { "sec-fetch-site": "cross-site" },
      });
      expect(res.status()).toBe(403);
    }
    expect(upstreamHits).toEqual([]);
  });

  test("allowed Host + foreign Origin on POST -> 403, zero upstream hits", async ({
    request,
  }) => {
    const res = await request.post("/api/blotter", {
      headers: {
        origin: "http://attacker.example",
        "content-type": "application/json",
      },
      data: "{}",
    });
    expect(res.status()).toBe(403);
    expect(upstreamHits).toEqual([]);
  });

  test("allowed Host + non-JSON mutation -> 403, zero upstream hits", async ({
    request,
  }) => {
    for (const contentType of ["text/plain", "application/x-www-form-urlencoded"]) {
      const res = await request.post("/api/blotter", {
        headers: {
          origin: `http://${ALLOWED_HOST}`,
          "content-type": contentType,
        },
        data: "x=1",
      });
      expect(res.status()).toBe(403);
    }
    expect(upstreamHits).toEqual([]);
  });

  test("allowed Host POST without Origin or same-origin marker -> 403, zero upstream hits", async ({
    request,
  }) => {
    const res = await request.post("/api/blotter", {
      headers: { "content-type": "application/json" },
      data: "{}",
    });
    expect(res.status()).toBe(403);
    expect(upstreamHits).toEqual([]);
  });

  test("legitimate same-origin GET reaches upstream", async ({ request }) => {
    const res = await request.get("/api/blotter", {
      headers: { "sec-fetch-site": "same-origin" },
    });
    expect(res.status()).toBe(200);
    expect(upstreamHits).toEqual(["GET /blotter"]);
  });

  test("legitimate same-origin JSON POST reaches upstream", async ({
    request,
  }) => {
    const res = await request.post("/api/blotter", {
      headers: {
        origin: `http://${ALLOWED_HOST}`,
        "content-type": "application/json",
      },
      data: "{}",
    });
    expect(res.status()).toBe(200);
    expect(upstreamHits).toEqual(["POST /blotter"]);
  });
});
