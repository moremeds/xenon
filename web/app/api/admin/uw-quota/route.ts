import { auth } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

import type { UwQuota } from "@/lib/operatorTypes";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const UW_BASE = "https://api.unusualwhales.com";
// Any UW endpoint returns the x-uw-* rate-limit headers; use a light one.
const PROBE_PATH = "/api/stock/SPY/stock-state";
// Burst guard only — the client drives cadence (30-min RTH auto + manual). This
// just dedupes near-simultaneous calls (multiple tabs / a manual click landing
// right after an auto-refresh) so we don't double-spend a UW hit.
const CACHE_TTL_MS = 15_000;

function num(h: Headers, k: string): number | null {
  const v = h.get(k);
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function parseUwQuotaHeaders(h: Headers, fetchedAt: string): UwQuota {
  return {
    configured: true,
    daily_count: num(h, "x-uw-daily-req-count"),
    daily_limit: num(h, "x-uw-token-req-limit"),
    minute_count: num(h, "x-uw-minute-req-counter"),
    minute_remaining: num(h, "x-uw-req-per-minute-remaining"),
    minute_reset_ms: num(h, "x-uw-req-per-minute-reset"),
    fetched_at: fetchedAt,
  };
}

let _cache: { data: UwQuota; ts: number } | null = null;

export async function GET(): Promise<Response> {
  // This route spends the server's UW_TOKEN daily quota on every cache miss, so
  // it must not be callable anonymously. `/api/*` is public at the Next
  // middleware (server-side page fetches carry no Clerk cookie), so the gate
  // lives here. The browser fetch from UwQuotaTile carries the session cookie.
  // Dev/E2E bypass mirrors the middleware flag.
  const authBypass =
    process.env.XENON_DISABLE_AUTH === "1" ||
    process.env.PLAYWRIGHT_DISABLE_AUTH === "1";
  if (!authBypass) {
    const { userId } = await auth();
    if (!userId) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
  }

  const token = process.env.UW_TOKEN;
  if (!token) {
    return NextResponse.json({
      configured: false,
      daily_count: null,
      daily_limit: null,
      minute_count: null,
      minute_remaining: null,
      minute_reset_ms: null,
      fetched_at: null,
    } satisfies UwQuota);
  }

  const now = Date.now();
  if (_cache && now - _cache.ts < CACHE_TTL_MS) {
    return NextResponse.json(_cache.data);
  }

  try {
    const res = await fetch(`${UW_BASE}${PROBE_PATH}`, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(8_000),
    });
    const data = parseUwQuotaHeaders(res.headers, new Date().toISOString());
    // If UW errored AND didn't return the rate headers, surface it as degraded.
    if (!res.ok && data.daily_count == null) {
      data.error = `UW probe HTTP ${res.status}`;
    }
    _cache = { data, ts: now };
    return NextResponse.json(data);
  } catch (err) {
    return NextResponse.json({
      configured: true,
      daily_count: null,
      daily_limit: null,
      minute_count: null,
      minute_remaining: null,
      minute_reset_ms: null,
      fetched_at: new Date().toISOString(),
      error: err instanceof Error ? err.message : "UW probe failed",
    } satisfies UwQuota);
  }
}
