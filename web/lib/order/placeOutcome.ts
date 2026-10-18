/** Place-outcome helpers shared by the Next order routes. Kept out of
 * lib/xenonApi so route tests that mock xenonFetch still load them. */

/** True when xenonFetch gave up waiting (AbortSignal.timeout). For an order
 * placement this is NOT a failure: FastAPI may still have reached IB. */
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

/** The uncertain body to return for a failed place call, or null when the
 * failure is a definite one. */
export function placeOutcomeUnknownBody(
  err: unknown,
): Record<string, unknown> | null {
  const body = (err as { body?: Record<string, unknown> | null } | null)?.body;
  if (body && body.reason_code === "PLACE_OUTCOME_UNKNOWN") return body;
  return isXenonTimeout(err) ? { ...PLACE_OUTCOME_UNKNOWN_BODY } : null;
}
