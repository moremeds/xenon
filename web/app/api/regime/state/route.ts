import { NextResponse } from "next/server";

import { getRequestId, setCacheResponseHeaders } from "@/lib/apiContracts";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

/**
 * GET /api/regime/state — proxies the new RegimeState surface from FastAPI.
 *
 * Distinct from the legacy /api/regime which returns CRI scan data. The
 * /state suffix avoids the path collision that was hit when both lived
 * at /regime — see test_legacy_regime_endpoint_not_shadowed in
 * src/xenon/api/tests/test_regime_routes.py.
 */
export async function GET(): Promise<Response> {
  const requestId = getRequestId();
  try {
    const upstream = await xenonFetch<Record<string, unknown>>(
      "/regime/state",
      {
        method: "GET",
        timeout: 10_000,
      },
    );
    const response = NextResponse.json(upstream);
    return setCacheResponseHeaders(response, {
      maxAgeSeconds: 30,
      staleWhileRevalidateSeconds: 60,
      requestId,
      cacheState: "HIT",
      tags: ["regime-state"],
    });
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "regime state fetch failed";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
