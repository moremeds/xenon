import { readReasonCode } from "@/lib/readReasonCode";
import { getReasonToast } from "@/lib/orderReasonCodes";

/** Derive a user-facing error string from a /api/orders/* JSON body.
 *
 * Precedence: reason_code → toast copy (also reads the FastAPI
 * {detail: {reason_code}} envelope via readReasonCode), then a string
 * `error` field, then a string `detail` field, then the caller's fallback.
 */
/** FastAPI's uncertain-place 502: IB may hold the order. Callers keep the
 * client_attempt_id (a resend of the same id never places twice) and offer
 * no retry. */
export function isPlaceOutcomeUnknown(
  body: Record<string, unknown> | null | undefined,
): boolean {
  return !!body && readReasonCode(body) === "PLACE_OUTCOME_UNKNOWN";
}

export function errorFromResponseBody(
  body: Record<string, unknown> | null | undefined,
  fallback: string,
): string {
  if (body && typeof body === "object") {
    const code = readReasonCode(body);
    if (code) {
      return getReasonToast(code).copy;
    }
    const err = body.error;
    if (typeof err === "string" && err.length > 0) return err;
    const detail = body.detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
  }
  return fallback;
}
