import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

const STALE_THRESHOLD_SECONDS = 600;

interface CacheMeta {
  last_refresh: string | null;
  age_seconds: number | null;
  is_stale: boolean;
  stale_threshold_seconds: number;
}

const EMPTY_DISCOVER = {
  discovery_time: "",
  alerts_analyzed: 0,
  candidates_found: 0,
  candidates: [],
};

function cacheMetaFromPayload(payload: Record<string, unknown> | null): CacheMeta {
  const raw =
    (typeof payload?._scanned_at === "string" && payload._scanned_at) ||
    (typeof payload?.discovery_time === "string" && payload.discovery_time) ||
    null;
  const ts = raw ? Date.parse(raw) : NaN;
  if (!Number.isFinite(ts)) {
    return {
      last_refresh: null,
      age_seconds: null,
      is_stale: true,
      stale_threshold_seconds: STALE_THRESHOLD_SECONDS,
    };
  }
  const ageSeconds = (Date.now() - ts) / 1000;
  return {
    last_refresh: new Date(ts).toISOString(),
    age_seconds: Math.round(ageSeconds),
    is_stale: ageSeconds > STALE_THRESHOLD_SECONDS,
    stale_threshold_seconds: STALE_THRESHOLD_SECONDS,
  };
}

export async function GET(): Promise<Response> {
  try {
    const data = await xenonFetch<Record<string, unknown>>("/discover", {
      method: "GET",
      timeout: 10_000,
    });
    return NextResponse.json({ ...data, cache_meta: cacheMetaFromPayload(data) });
  } catch {
    return NextResponse.json({
      ...EMPTY_DISCOVER,
      cache_meta: cacheMetaFromPayload(null),
    });
  }
}

export async function POST(): Promise<Response> {
  try {
    const data = await xenonFetch<Record<string, unknown>>("/discover", {
      method: "POST",
      timeout: 130_000,
    });
    return NextResponse.json({ ...data, cache_meta: cacheMetaFromPayload(data) });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Discover sync failed";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
