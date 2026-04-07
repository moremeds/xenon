import { NextResponse } from "next/server";
import { readFile } from "fs/promises";
import { join } from "path";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";

export const runtime = "nodejs";

const DATA_DIR = join(process.cwd(), "..", "data");
const CACHE_PATH = join(DATA_DIR, "futu_portfolio.json");

/**
 * GET /api/futu/portfolio
 *
 * Read-only Futu positions + account snapshot.
 *
 * Strategy: try FastAPI first (which serves from its singleton's in-memory
 * cache), fall back to the on-disk JSON written by the last sync. Never
 * blocks on OpenD — if the user wants fresh data they POST to /api/futu/sync.
 */
export async function GET() {
  // Try FastAPI first (fast, in-memory).
  try {
    const data = await xenonFetch("/futu/portfolio", { method: "GET" });
    return NextResponse.json(data);
  } catch (err) {
    // 404 from FastAPI means "never synced" — fall through to disk.
    if (!(err instanceof XenonApiError) || err.status !== 404) {
      // Any other failure (FastAPI down, 5xx, etc.) also falls through so
      // the UI keeps working off the last cached snapshot.
      console.warn("[futu/portfolio] FastAPI unreachable, serving disk cache:", err);
    }
  }

  try {
    const raw = await readFile(CACHE_PATH, "utf-8");
    const data = JSON.parse(raw);
    return NextResponse.json({ ...data, is_stale: true });
  } catch (err) {
    return NextResponse.json(
      {
        error: "Futu portfolio not yet synced. Start Futu OpenD and POST /api/futu/sync.",
        detail: err instanceof Error ? err.message : String(err),
      },
      { status: 404 },
    );
  }
}

/**
 * POST /api/futu/portfolio
 *
 * Force a live sync against OpenD. Delegates to FastAPI's POST /futu/sync
 * which holds the singleflight lock.
 */
export async function POST() {
  try {
    const data = await xenonFetch("/futu/sync", { method: "POST" });
    return NextResponse.json(data);
  } catch (err) {
    if (err instanceof XenonApiError) {
      return NextResponse.json(
        { error: err.message, status: err.status },
        { status: err.status },
      );
    }
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 },
    );
  }
}
