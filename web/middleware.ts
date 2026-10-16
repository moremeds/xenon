import { NextRequest, NextResponse } from "next/server";

/**
 * Browser-origin guard for /api/** requests (CSRF / DNS-rebinding).
 *
 * Prod is tailnet-only: reachability is the trust boundary, so /api/** has no
 * user-auth layer. But a *browser* on an allowed client can still be tricked
 * into firing requests at this app — a malicious page can POST a "simple"
 * request (text/plain / urlencoded / multipart) cross-origin without a CORS
 * preflight, and DNS rebinding can point a foreign Host at this server (the
 * rebound page then reads /api GETs under its own origin — so the Host check
 * applies to EVERY method, including GET/HEAD).
 *
 * All /api/** requests: Host ∈ XENON_ALLOWED_HOSTS (comma list; default
 * localhost:3000, 127.0.0.1:3000, localhost:3200, 127.0.0.1:3200).
 *
 * Non-GET/HEAD additionally requires ALL of:
 *   1. Origin's host matches the request Host exactly; when Origin is absent
 *      (curl, some navigations) Sec-Fetch-Site: same-origin is required;
 *   2. Content-Type is application/json — a value no cross-origin form or
 *      simple request can send. multipart/form-data is allowed only for the
 *      routes listed in MULTIPART_API_PATHS (currently none).
 *
 * Rejections happen before any route/FastAPI call. Server-to-server callers
 * must hit FastAPI directly with a credential — they never go through Next,
 * so this browser-shaped contract is safe to enforce.
 */
export const DEFAULT_ALLOWED_HOSTS =
  "localhost:3000,127.0.0.1:3000,localhost:3200,127.0.0.1:3200";

/** No /api route currently consumes multipart bodies; opt-in by path here. */
export const MULTIPART_API_PATHS: ReadonlySet<string> = new Set();

export function allowedHosts(
  envValue: string | undefined = process.env.XENON_ALLOWED_HOSTS,
): Set<string> {
  const raw = envValue?.trim() ? envValue : DEFAULT_ALLOWED_HOSTS;
  return new Set(
    raw
      .split(",")
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean),
  );
}

function forbidden(reason: string): NextResponse {
  return NextResponse.json(
    { error: "Forbidden", reason },
    { status: 403 },
  );
}

export default function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const method = request.method.toUpperCase();

  if (!pathname.startsWith("/api/")) {
    return NextResponse.next();
  }

  // Host allowlist applies to EVERY method — DNS rebinding can otherwise read
  // /api GETs through a rebound same-origin page.
  const host = (request.headers.get("host") ?? "").toLowerCase();
  if (!allowedHosts().has(host)) {
    return forbidden("host_not_allowed");
  }

  if (method === "GET" || method === "HEAD") {
    return NextResponse.next();
  }

  const origin = request.headers.get("origin");
  if (origin !== null) {
    let originHost: string;
    try {
      originHost = new URL(origin).host.toLowerCase();
    } catch {
      return forbidden("bad_origin");
    }
    if (originHost !== host) {
      return forbidden("origin_mismatch");
    }
  } else {
    const secFetchSite = (
      request.headers.get("sec-fetch-site") ?? ""
    ).toLowerCase();
    if (secFetchSite !== "same-origin") {
      return forbidden("missing_origin");
    }
  }

  const contentType = (request.headers.get("content-type") ?? "")
    .split(";")[0]
    .trim()
    .toLowerCase();
  const isJson = contentType === "application/json";
  const isAllowedMultipart =
    contentType === "multipart/form-data" && MULTIPART_API_PATHS.has(pathname);
  if (!isJson && !isAllowedMultipart) {
    return forbidden("content_type");
  }

  return NextResponse.next();
}

export const config = {
  matcher: "/api/:path*",
};
