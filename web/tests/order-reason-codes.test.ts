import { describe, it, expect } from "vitest";
import { ORDER_REASON_CODES, getReasonToast } from "../lib/orderReasonCodes";

// Source of truth: Python `ReasonCode` StrEnum in src/xenon/execution/preflight.py.
// Keep this list in lockstep with the enum. A future F6 follow-up may replace
// this hardcoded list with a build-time generator (option (b) in the plan).
const PYTHON_REASON_CODES = [
  // F2 — preflight
  "UNIVERSE_UNKNOWN",
  "INDEX_HAS_NO_STOCK",
  "INSUFFICIENT_SHARES",
  "INSUFFICIENT_CASH",
  "INDEX_CALL_UNCOVERED",
  "ETF_CALL_UNCOVERED",
  // F3 — quote gate
  "STALE_QUOTE",
  "LIMIT_OUT_OF_BAND",
  "LIMIT_OFF_TICK",
  // F4 — idempotency
  "ATTEMPT_ID_TERMINAL",
  // F5 — cancel/modify failure classification
  "IB_CONNECTION",
  "OWNERSHIP",
  "IB_REJECT",
  "MODIFY_STALE",
  "MODIFY_SEQUENCE_REQUIRED",
  "ORDER_NOT_FOUND",
  // F7 — pending timeout
  "PENDING_TIMEOUT",
];

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
});
