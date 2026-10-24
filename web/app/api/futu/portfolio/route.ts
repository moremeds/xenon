import { NextResponse } from "next/server";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";
import { forwardRequestQuery } from "@/lib/forwardRequestQuery";

export const runtime = "nodejs";

/**
 * GET /api/futu/portfolio
 *
 * Read-only Futu positions + account snapshot, proxied from FastAPI
 * (which serves from its singleton's in-memory cache). Never blocks on
 * OpenD — if the user wants fresh data they POST to /api/futu/sync.
 * No disk fallback: Postgres/FastAPI is the runtime source of truth.
 */
export async function GET(req: Request) {
  const qs = forwardRequestQuery(req);
  try {
    const data = await xenonFetch(`/futu/portfolio${qs}`, { method: "GET" });
    return NextResponse.json(data);
  } catch (err) {
    // 404 from FastAPI means "never synced".
    if (err instanceof XenonApiError && err.status === 404) {
      return NextResponse.json(
        {
          error:
            "Futu portfolio not yet synced. Start Futu OpenD and POST /api/futu/sync.",
        },
        { status: 404 },
      );
    }
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: `Futu portfolio unavailable: ${message}` },
      { status: 502 },
    );
  }
}

/**
 * POST /api/futu/portfolio
 *
 * Force a live sync against OpenD. Delegates to FastAPI's POST /futu/sync
 * which holds the singleflight lock.
 */
export async function POST(req: Request) {
  const qs = forwardRequestQuery(req);
  try {
    const data = await xenonFetch(`/futu/sync${qs}`, { method: "POST" });
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
