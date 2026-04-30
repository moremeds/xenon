/**
 * Regime-gate response handling for order submit calls.
 *
 * Backend (Phase 3) returns:
 * - 409 with `reason_code: "REGIME_BLOCK"` when the order would create new
 *   exposure that the regime tier disallows. The caller must prompt the
 *   user for an override reason (≥10 chars), then re-POST with
 *   `override: true` + `override_reason` in the body.
 * - 422 with `reason_code: "REGIME_RESIZE_REQUIRED"` when a TIER_2 throttle
 *   is in effect and the order's max_loss exceeds the cap. The caller
 *   prompts the user to trim contract count, then re-POSTs with the
 *   smaller quantity.
 *
 * Both response shapes are structurally identical except for `decision`
 * and the throttle-only fields. This module owns the parsing.
 */

export type RegimeBlockResponse = {
  detail: string;
  reason_code: "REGIME_BLOCK";
  decision: "block";
  binding_tier: string;
  binding_side: string;
  vcg_tier: string;
  cri_tier: string;
  override_required: true;
  override_min_reason_chars: number;
  modify_path?: boolean;
  delta_quantity?: number;
};

export type RegimeResizeResponse = {
  detail: string;
  reason_code: "REGIME_RESIZE_REQUIRED";
  decision: "resize_required";
  binding_tier: string;
  binding_side: string;
  max_loss_usd: number | null;
  max_loss_cap_usd: number;
  cover_ratio: number;
  modify_path?: boolean;
  delta_quantity?: number;
};

export type RegimeGateOutcome =
  | { kind: "ok" }
  | { kind: "block"; payload: RegimeBlockResponse }
  | { kind: "resize"; payload: RegimeResizeResponse }
  | { kind: "other"; status: number; body: unknown };

/**
 * Inspect a fetch Response from /api/orders/place or /api/orders/modify.
 * Caller should already have awaited the response; this reads the body
 * and classifies it.
 */
export async function parseRegimeGateResponse(
  res: Response,
): Promise<RegimeGateOutcome> {
  if (res.ok) return { kind: "ok" };
  let body: unknown = null;
  try {
    body = await res.clone().json();
  } catch {
    return { kind: "other", status: res.status, body: null };
  }
  const obj = body as Record<string, unknown> | null;
  const reasonCode = obj?.reason_code;
  if (res.status === 409 && reasonCode === "REGIME_BLOCK") {
    return { kind: "block", payload: obj as unknown as RegimeBlockResponse };
  }
  if (res.status === 422 && reasonCode === "REGIME_RESIZE_REQUIRED") {
    return { kind: "resize", payload: obj as unknown as RegimeResizeResponse };
  }
  return { kind: "other", status: res.status, body: obj };
}

/**
 * Build the body fields for a retry that overrides the regime block.
 * Caller is responsible for spreading these onto the original body and
 * re-POSTing.
 */
export function buildRegimeOverrideFields(reason: string): {
  override: true;
  override_reason: string;
} {
  return { override: true, override_reason: reason.trim() };
}

export function isRegimeOverrideReasonValid(
  reason: string,
  minChars = 10,
): boolean {
  return reason.trim().length >= minChars;
}

/**
 * Compute the largest contract count whose max_loss stays within the
 * gate's cap. Used by the resize prompt to pre-fill a "trim to fit"
 * suggestion.
 *
 * Linear scaling: max_loss is proportional to contract count for
 * defined-risk structures. If max_loss_usd is null (unbounded), the
 * caller has no defined-risk basis for resizing — return null.
 */
export function suggestResizeQuantity(
  payload: RegimeResizeResponse,
  currentQuantity: number,
): number | null {
  if (payload.max_loss_usd == null || payload.max_loss_usd <= 0) return null;
  if (currentQuantity <= 0) return null;
  const ratio = payload.max_loss_cap_usd / payload.max_loss_usd;
  const trimmed = Math.floor(currentQuantity * ratio);
  return trimmed > 0 ? trimmed : null;
}
