import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

const VALID_ACCOUNTS = new Set(["ib", "futu"]);
const STALE_THRESHOLD_SECONDS = 600;

interface CacheMeta {
  last_refresh: string | null;
  age_seconds: number | null;
  is_stale: boolean;
  stale_threshold_seconds: number;
}

function resolveAccount(req: Request): string | null {
  const account = new URL(req.url).searchParams.get("account") ?? "ib";
  return VALID_ACCOUNTS.has(account) ? account : null;
}

function buildCacheMeta(analysisTime: string | undefined): CacheMeta {
  if (!analysisTime) {
    return {
      last_refresh: null,
      age_seconds: null,
      is_stale: true,
      stale_threshold_seconds: STALE_THRESHOLD_SECONDS,
    };
  }
  const parsed = new Date(analysisTime);
  const ageSeconds = Number.isFinite(parsed.getTime())
    ? (Date.now() - parsed.getTime()) / 1000
    : null;
  return {
    last_refresh: Number.isFinite(parsed.getTime())
      ? parsed.toISOString()
      : null,
    age_seconds: ageSeconds !== null ? Math.round(ageSeconds) : null,
    is_stale: ageSeconds !== null ? ageSeconds > STALE_THRESHOLD_SECONDS : true,
    stale_threshold_seconds: STALE_THRESHOLD_SECONDS,
  };
}

function emptyPayload(account: string): Record<string, unknown> {
  return {
    analysis_time: "",
    account,
    positions_scanned: 0,
    skipped_unsupported: 0,
    supports: [],
    against: [],
    mixed: [],
    non_directional: [],
    neutral: [],
    cache_meta: buildCacheMeta(undefined),
  };
}

async function proxy(
  method: "GET" | "POST",
  account: string,
): Promise<Response> {
  try {
    const data = await xenonFetch(`/flow-analysis?account=${account}`, {
      method,
      timeout: method === "POST" ? 130_000 : 5_000,
    });
    const cache_meta = buildCacheMeta(
      (data as { analysis_time?: string }).analysis_time,
    );
    return NextResponse.json({ ...(data as object), cache_meta });
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Flow analysis unavailable";
    const res = NextResponse.json(emptyPayload(account));
    res.headers.set("X-Sync-Warning", `Xenon API unavailable - ${message}`);
    return res;
  }
}

export async function GET(req: Request): Promise<Response> {
  const account = resolveAccount(req);
  if (!account) {
    return NextResponse.json({ error: "Unknown account" }, { status: 400 });
  }
  return proxy("GET", account);
}

export async function POST(req: Request): Promise<Response> {
  const account = resolveAccount(req);
  if (!account) {
    return NextResponse.json({ error: "Unknown account" }, { status: 400 });
  }
  return proxy("POST", account);
}
