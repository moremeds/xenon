// Single-source UI toast copy for order reason codes. Must stay in parity with
// the Python `ReasonCode` StrEnum in `src/xenon/execution/preflight.py`.
// See F6 in the F5→F7 plan.

export type ReasonSeverity = "error" | "warn" | "info";

export interface ReasonToast {
  severity: ReasonSeverity;
  copy: string;
}

export const ORDER_REASON_CODES: Record<string, ReasonToast> = {
  // F2 — preflight
  UNIVERSE_UNKNOWN: { severity: "error", copy: "Ticker not in V1 universe." },
  INDEX_HAS_NO_STOCK: {
    severity: "error",
    copy: "Index options can't trade as stock (SPX/NDX/RUT).",
  },
  INSUFFICIENT_SHARES: {
    severity: "error",
    copy: "SELL exceeds held shares (including working orders).",
  },
  INSUFFICIENT_CASH: {
    severity: "error",
    copy: "Cash-secured put exceeds available funds.",
  },
  INDEX_CALL_UNCOVERED: {
    severity: "error",
    copy: "Short index call requires long-call cover (same expiry).",
  },
  ETF_CALL_UNCOVERED: {
    severity: "error",
    copy: "Short call uncovered after accounting for working orders.",
  },
  // F3 — quote gate
  STALE_QUOTE: { severity: "error", copy: "Quote expired; refreshing." },
  LIMIT_OUT_OF_BAND: {
    severity: "warn",
    copy: "Limit too far from market. Acknowledge to override.",
  },
  LIMIT_OFF_TICK: {
    severity: "error",
    copy: "Price not on contract tick grid.",
  },
  // F4 — idempotency
  ATTEMPT_ID_TERMINAL: {
    severity: "info",
    copy: "Previous attempt ended. Rotating id.",
  },
  // F5 — cancel/modify failure classification
  IB_CONNECTION: { severity: "error", copy: "IB connection lost — retry." },
  OWNERSHIP: { severity: "error", copy: "Order owned by another session." },
  IB_REJECT: {
    severity: "error",
    copy: "IB rejected the order. See details.",
  },
  MODIFY_STALE: {
    severity: "error",
    copy: "Modify sequence stale; refresh and retry.",
  },
  MODIFY_SEQUENCE_REQUIRED: {
    severity: "error",
    copy: "Modify requires a sequence number.",
  },
  ORDER_NOT_FOUND: { severity: "error", copy: "Order no longer exists." },
  // F7 — pending timeout
  PENDING_TIMEOUT: {
    severity: "warn",
    copy: "Order submission timed out before IB acknowledged.",
  },
};

const FALLBACK: ReasonToast = {
  severity: "error",
  copy: "Unknown error — see logs.",
};

export function getReasonToast(code: string | null | undefined): ReasonToast {
  if (!code) return FALLBACK;
  return ORDER_REASON_CODES[code] ?? FALLBACK;
}
