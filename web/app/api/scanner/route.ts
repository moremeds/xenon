import { NextResponse } from "next/server";
import { readFile } from "fs/promises";
import { statSync } from "fs";
import { join } from "path";
export const runtime = "nodejs";

const CACHE_PATH = join(process.cwd(), "..", "data", "trend_scan.json");
const STALE_THRESHOLD_SECONDS = 600;

interface CacheMeta {
  last_refresh: string | null;
  age_seconds: number | null;
  is_stale: boolean;
  stale_threshold_seconds: number;
}

function buildCacheMeta(filePath: string): CacheMeta {
  try {
    const s = statSync(filePath);
    const ageSeconds = (Date.now() - s.mtime.getTime()) / 1000;
    return {
      last_refresh: s.mtime.toISOString(),
      age_seconds: Math.round(ageSeconds),
      is_stale: ageSeconds > STALE_THRESHOLD_SECONDS,
      stale_threshold_seconds: STALE_THRESHOLD_SECONDS,
    };
  } catch {
    return {
      last_refresh: null,
      age_seconds: null,
      is_stale: true,
      stale_threshold_seconds: STALE_THRESHOLD_SECONDS,
    };
  }
}

export async function GET(): Promise<Response> {
  try {
    const raw = await readFile(CACHE_PATH, "utf-8");
    const data = JSON.parse(raw);
    const cache_meta = buildCacheMeta(CACHE_PATH);
    return NextResponse.json({ ...data, cache_meta });
  } catch {
    const cache_meta = buildCacheMeta(CACHE_PATH);
    return NextResponse.json({
      scan_id: "",
      scan_timestamp: "",
      market_context: { spy_close: 0, vix_close: 0, regime: "unknown" },
      universe_size: 0,
      stage_a_survivors: 0,
      stage_b_survivors: 0,
      candidates: [],
      cache_meta,
    });
  }
}
