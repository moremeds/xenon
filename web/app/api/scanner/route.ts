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

const EMPTY_SCAN = {
  scan_id: "",
  scan_timestamp: "",
  market_context: { spy_close: 0, vix_close: 0, regime: "unknown" },
  universe_size: 0,
  stage_a_survivors: 0,
  stage_b_survivors: 0,
  candidates: [],
};

function cacheMetaFromPayload(payload: Record<string, unknown> | null): CacheMeta {
  const raw =
    (typeof payload?._scanned_at === "string" && payload._scanned_at) ||
    (typeof payload?.scan_timestamp === "string" && payload.scan_timestamp) ||
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
    const data = await xenonFetch<Record<string, unknown>>("/scan", {
      method: "GET",
      timeout: 10_000,
    });
    return NextResponse.json({ ...data, cache_meta: cacheMetaFromPayload(data) });
  } catch {
    return NextResponse.json({
      ...EMPTY_SCAN,
      cache_meta: cacheMetaFromPayload(null),
    });
  }
}
