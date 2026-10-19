/** Place-outcome helpers shared by the Next order routes (PR-07, OP-1).
 * Kept out of lib/xenonApi so route tests that mock xenonFetch load them. */
import { ORDER_REASON_CODES } from "@/lib/orderReasonCodes";
import { readReasonCode } from "@/lib/readReasonCode";

/** True when xenonFetch gave up waiting (AbortSignal.timeout). */
export function isXenonTimeout(err: unknown): boolean {
  const name = (err as { name?: unknown } | null)?.name;
  return name === "TimeoutError" || name === "AbortError";
}

/** Same top-level body as FastAPI's uncertain-place 502. */
export const PLACE_OUTCOME_UNKNOWN_BODY = {
  reason_code: "PLACE_OUTCOME_UNKNOWN",
  detail:
    "Order outcome unknown — check working orders before placing it again. Do not resubmit.",
} as const;

/** The uncertain body for a place call that failed AFTER it was dispatched
 * to FastAPI, or null when the failure proves nothing was placed.
 *
 * Terminal (null): an HTTP 4xx, or a 5xx carrying a known rejection
 * reason_code (IB_REJECT, SUBPROCESS_ERROR, ...). Everything else — a
 * timeout, socket reset (TypeError), unreadable body, a 5xx without a known
 * reason_code — means FastAPI may have reached IB: uncertain. */
export function placeOutcomeUnknownBody(
  err: unknown,
): Record<string, unknown> | null {
  const e = err as {
    status?: unknown;
    body?: Record<string, unknown> | null;
  } | null;
  const code = readReasonCode(e?.body);
  if (code === "PLACE_OUTCOME_UNKNOWN") return e!.body as Record<string, unknown>;
  if (typeof e?.status === "number") {
    if (e.status < 500) return null;
    if (code && code in ORDER_REASON_CODES) return null;
  }
  return { ...PLACE_OUTCOME_UNKNOWN_BODY };
}
