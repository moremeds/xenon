import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";
import { getRequestId, setNoStoreResponseHeaders } from "@/lib/apiContracts";

export const runtime = "nodejs";

type PerformancePayload = Record<string, unknown>;

function jsonWithNoStore(
  data: unknown,
  requestId: string,
  status = 200,
): NextResponse {
  return setNoStoreResponseHeaders(
    NextResponse.json(data, { status }),
    requestId,
  );
}

/** Forwards `?broker=IB|FUTU` + `?period=1M|3M|YTD|All` to FastAPI's
 *  `/performance`. FastAPI owns the TTL cache (market-aware 60s open /
 *  30min closed) — no second-level cache here, since duplicating it leads
 *  to stale-after-write surprises. */
export async function GET(request: NextRequest): Promise<Response> {
  const requestId = getRequestId();
  const broker = request.nextUrl.searchParams.get("broker") ?? "IB";
  const period = request.nextUrl.searchParams.get("period") ?? "YTD";
  const path = `/performance?broker=${encodeURIComponent(broker)}&period=${encodeURIComponent(period)}`;
  try {
    const data = await xenonFetch<PerformancePayload>(path, {
      timeout: 180_000,
    });
    return jsonWithNoStore(data, requestId);
  } catch (error) {
    if (error instanceof XenonApiError) {
      // Preserve upstream status (409 cross-env conflict, 503 OpenD down, etc.)
      return jsonWithNoStore(
        { error: error.message, detail: error.detail },
        requestId,
        error.status,
      );
    }
    const message =
      error instanceof Error ? error.message : "performance fetch failed";
    return jsonWithNoStore({ error: message }, requestId, 502);
  }
}
