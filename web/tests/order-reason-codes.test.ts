import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";
import { ORDER_REASON_CODES, getReasonToast } from "../lib/orderReasonCodes";

// Source of truth: the Python `ReasonCode` StrEnum in
// src/xenon/execution/preflight.py — read directly so a new enum member
// fails this test until matching TS copy exists (no codegen, no drift).
const PREFLIGHT_PY = fileURLToPath(
  new URL("../../src/xenon/execution/preflight.py", import.meta.url),
);

function pythonReasonCodes(): string[] {
  const src = readFileSync(PREFLIGHT_PY, "utf8");
  const body = src.match(/class ReasonCode\(StrEnum\):([\s\S]*?)(?=\nclass )/m);
  if (!body) throw new Error("ReasonCode class not found in preflight.py");
  const codes: string[] = [];
  for (const m of body[1].matchAll(/^\s+([A-Z][A-Z0-9_]*)\s*=\s*"([A-Z0-9_]+)"\s*$/gm)) {
    if (m[1] !== m[2]) {
      throw new Error(`ReasonCode member ${m[1]} does not equal its value ${m[2]}`);
    }
    codes.push(m[1]);
  }
  return codes;
}

const PYTHON_REASON_CODES = pythonReasonCodes();

describe("orderReasonCodes parity", () => {
  it("test_every_python_code_has_ts_copy", () => {
    const tsKeys = Object.keys(ORDER_REASON_CODES).sort();
    const pyKeys = [...PYTHON_REASON_CODES].sort();

    const missingInTs = pyKeys.filter((k) => !tsKeys.includes(k));
    const extraInTs = tsKeys.filter((k) => !pyKeys.includes(k));

    expect(missingInTs).toEqual([]);
    expect(extraInTs).toEqual([]);

    // Every entry must have both severity and non-empty copy.
    for (const code of tsKeys) {
      const entry = ORDER_REASON_CODES[code];
      expect(["error", "warn", "info"]).toContain(entry.severity);
      expect(entry.copy.length).toBeGreaterThan(0);
    }
  });
});

describe("getReasonToast fallback", () => {
  it("test_no_unknown_codes_fall_through", () => {
    const fallback = getReasonToast("BOGUS_CODE");
    expect(fallback.severity).toBe("error");
    expect(fallback.copy).toBe("Unknown error — see logs.");

    expect(getReasonToast("")).toEqual(fallback);
    expect(getReasonToast(undefined as unknown as string)).toEqual(fallback);
    expect(getReasonToast(null as unknown as string)).toEqual(fallback);
  });

  it("returns the mapped toast for a known code", () => {
    const toast = getReasonToast("PENDING_TIMEOUT");
    expect(toast.severity).toBe("warn");
    expect(toast.copy).toBe(
      "Order submission timed out before IB acknowledged.",
    );
  });

  it("MODIFY_PENDING says what the user can do (retry later or cancel)", () => {
    const toast = getReasonToast("MODIFY_PENDING");
    expect(toast.severity).toBe("warn");
    expect(toast.copy).toContain("not confirmed by IB yet");
    expect(toast.copy).toContain("Retry in about two minutes");
    expect(toast.copy).toContain("cancel the order");
    // Recovery needs IB to report the order again, not just any sync.
    expect(toast.copy).not.toMatch(/wait for the order to sync/i);
  });
});
