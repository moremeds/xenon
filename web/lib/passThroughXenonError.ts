/**
 * Shared helper for Next.js /api/orders/* routes — preserves upstream
 * XenonApiError (status + JSON body) verbatim, and collapses all other
 * unexpected errors to a 500 with { error:"internal", request_id }.
 *
 * Keeping this in one place guarantees FastAPI reason_code responses
 * (e.g. STALE_QUOTE, IB_CONNECTION, MODIFY_STALE, ATTEMPT_ID_TERMINAL)
 * reach the browser without being collapsed or reshaped.
 */
import { NextResponse } from "next/server";
import { XenonApiError } from "./xenonApi";

export function passThroughXenonError(
  err: unknown,
  requestId: string,
): NextResponse {
  if (err instanceof XenonApiError) {
    // Preserve upstream JSON body verbatim (includes detail.reason_code,
    // detail.applied, etc.). Fall back to { detail: <string> } when the
    // upstream response had no JSON body.
    const payload: Record<string, unknown> =
      err.body && typeof err.body === "object"
        ? err.body
        : { detail: err.detail };
    const res = NextResponse.json(payload, { status: err.status });
    res.headers.set("X-Request-Id", requestId);
    return res;
  }
  // Unknown error: log (no stack to client) and return 500 with request_id.
  console.error(`[request_id=${requestId}]`, err);
  const res = NextResponse.json(
    { error: "internal", request_id: requestId },
    { status: 500 },
  );
  res.headers.set("X-Request-Id", requestId);
  return res;
}
