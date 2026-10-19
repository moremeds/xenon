/**
 * One browser-side entry for POST /api/orders/place (PR-07, OP-1).
 *
 * An uncertain outcome — FastAPI/Next 502 PLACE_OUTCOME_UNKNOWN, a network
 * failure after dispatch, an unreadable body, or a 5xx without a known
 * reason_code — means IB may hold the order. The attempt is then marked
 * uncertain: its client_attempt_id is kept and every later submit from the
 * form is refused here, so no click can place a second order. Only a 4xx or
 * a 5xx with an explicit (known) rejection reason_code is terminal.
 */
import { ORDER_REASON_CODES } from "@/lib/orderReasonCodes";
import { readReasonCode } from "@/lib/readReasonCode";
import { errorFromResponseBody } from "./responseError";

export const PLACE_UNCERTAIN_COPY =
  ORDER_REASON_CODES.PLACE_OUTCOME_UNKNOWN.copy;

/** Combo replacement (cancel + place) whose place outcome is unknown. */
export const REPLACEMENT_UNCERTAIN_COPY =
  "Replacement status unknown — check working orders; do not resubmit.";

export interface PlaceAttempt {
  id: string;
  isUncertain: () => boolean;
  markSubmitted: () => void;
  markTerminal: () => void;
  markUncertain: () => void;
}

export type PlaceOutcome =
  | { kind: "ok"; json: Record<string, unknown> }
  | { kind: "uncertain"; message: string }
  | { kind: "terminal"; message: string };

/** Uncertain unless the response proves the order was not placed. */
export function isUncertainPlaceResponse(
  status: number,
  body: Record<string, unknown> | null,
): boolean {
  const code = readReasonCode(body);
  if (code === "PLACE_OUTCOME_UNKNOWN") return true;
  if (status < 500) return false;
  return !(code && code in ORDER_REASON_CODES);
}

export async function submitPlaceOrder(
  attempt: PlaceAttempt,
  body: Record<string, unknown>,
): Promise<PlaceOutcome> {
  if (attempt.isUncertain()) {
    return { kind: "uncertain", message: PLACE_UNCERTAIN_COPY };
  }
  attempt.markSubmitted();
  let res: Response;
  try {
    res = await fetch("/api/orders/place", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, client_attempt_id: attempt.id }),
    });
  } catch {
    // Sent or not, we cannot tell: the request may have reached IB.
    attempt.markUncertain();
    return { kind: "uncertain", message: PLACE_UNCERTAIN_COPY };
  }
  let json: Record<string, unknown> | null = null;
  try {
    const parsed: unknown = await res.json();
    json = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
  } catch {
    json = null;
  }
  if (res.ok && json) {
    attempt.markTerminal();
    return { kind: "ok", json };
  }
  if (res.ok || isUncertainPlaceResponse(res.status, json)) {
    attempt.markUncertain();
    return { kind: "uncertain", message: PLACE_UNCERTAIN_COPY };
  }
  attempt.markTerminal();
  return {
    kind: "terminal",
    message: errorFromResponseBody(json, "Order placement failed"),
  };
}
