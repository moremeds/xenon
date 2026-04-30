import { describe, expect, it } from "vitest";
import {
  buildRegimeOverrideFields,
  isRegimeOverrideReasonValid,
  parseRegimeGateResponse,
  suggestResizeQuantity,
  type RegimeBlockResponse,
  type RegimeResizeResponse,
} from "@/lib/order/regimeGate";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("parseRegimeGateResponse", () => {
  it("returns ok for 2xx", async () => {
    const r = jsonResponse(200, { submission_id: "abc" });
    const result = await parseRegimeGateResponse(r);
    expect(result.kind).toBe("ok");
  });

  it("classifies 409 REGIME_BLOCK", async () => {
    const r = jsonResponse(409, {
      detail: "TIER_1 — non-hedge entries blocked",
      reason_code: "REGIME_BLOCK",
      decision: "block",
      binding_tier: "TIER_1",
      binding_side: "cri",
      vcg_tier: "NORMAL",
      cri_tier: "TIER_1",
      override_required: true,
      override_min_reason_chars: 10,
    });
    const result = await parseRegimeGateResponse(r);
    expect(result.kind).toBe("block");
    if (result.kind === "block") {
      expect(result.payload.binding_tier).toBe("TIER_1");
      expect(result.payload.override_min_reason_chars).toBe(10);
    }
  });

  it("classifies 422 REGIME_RESIZE_REQUIRED", async () => {
    const r = jsonResponse(422, {
      detail: "TIER_2 throttle: max loss exceeds cap",
      reason_code: "REGIME_RESIZE_REQUIRED",
      decision: "resize_required",
      binding_tier: "TIER_2",
      binding_side: "vcg",
      max_loss_usd: 500,
      max_loss_cap_usd: 125,
      cover_ratio: 1.25,
    });
    const result = await parseRegimeGateResponse(r);
    expect(result.kind).toBe("resize");
    if (result.kind === "resize") {
      expect(result.payload.max_loss_cap_usd).toBe(125);
      expect(result.payload.cover_ratio).toBe(1.25);
    }
  });

  it("falls through to other for non-regime errors", async () => {
    const r = jsonResponse(400, {
      detail: "bad request",
      reason_code: "BAD_BODY",
    });
    const result = await parseRegimeGateResponse(r);
    expect(result.kind).toBe("other");
    if (result.kind === "other") {
      expect(result.status).toBe(400);
    }
  });

  it("falls through on 409 with non-regime reason_code", async () => {
    const r = jsonResponse(409, {
      detail: "attempt terminal",
      reason_code: "ATTEMPT_ID_TERMINAL",
    });
    const result = await parseRegimeGateResponse(r);
    expect(result.kind).toBe("other");
  });
});

describe("isRegimeOverrideReasonValid", () => {
  it("rejects whitespace-only", () => {
    expect(isRegimeOverrideReasonValid("          ")).toBe(false);
  });

  it("accepts ≥10 chars after trim", () => {
    expect(isRegimeOverrideReasonValid("contrarian play x")).toBe(true);
  });

  it("rejects under-min", () => {
    expect(isRegimeOverrideReasonValid("short")).toBe(false);
  });

  it("respects custom min", () => {
    expect(isRegimeOverrideReasonValid("hi", 2)).toBe(true);
  });
});

describe("buildRegimeOverrideFields", () => {
  it("trims reason whitespace", () => {
    expect(buildRegimeOverrideFields("  reason text  ")).toEqual({
      override: true,
      override_reason: "reason text",
    });
  });
});

describe("suggestResizeQuantity", () => {
  const payload: RegimeResizeResponse = {
    detail: "x",
    reason_code: "REGIME_RESIZE_REQUIRED",
    decision: "resize_required",
    binding_tier: "TIER_2",
    binding_side: "vcg",
    max_loss_usd: 500,
    max_loss_cap_usd: 125,
    cover_ratio: 1.25,
  };

  it("scales linearly to fit cap", () => {
    // 4 contracts → 500 max loss; 125 cap is 1/4 → 1 contract suggested
    expect(suggestResizeQuantity(payload, 4)).toBe(1);
  });

  it("returns null when cap rounds below 1 contract", () => {
    expect(suggestResizeQuantity({ ...payload, max_loss_cap_usd: 50 }, 1)).toBe(
      null,
    );
  });

  it("returns null when max_loss_usd is unbounded", () => {
    expect(suggestResizeQuantity({ ...payload, max_loss_usd: null }, 4)).toBe(
      null,
    );
  });

  it("returns null on zero quantity", () => {
    expect(suggestResizeQuantity(payload, 0)).toBe(null);
  });
});
