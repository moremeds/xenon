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
  INVALID_ORDER_BODY: {
    severity: "error",
    copy: "Order request is malformed. Refresh and rebuild the ticket.",
  },
  // F3 — quote gate
  STALE_QUOTE: { severity: "error", copy: "Quote expired; refreshing." },
  OPTION_MARKET_CLOSED: {
    severity: "error",
    copy: "Equity options can only be traded during regular market hours.",
  },
  QUOTE_CONTRACT_MISMATCH: {
    severity: "error",
    copy: "Quote contract mismatch. Refresh the quote and retry.",
  },
  QUOTE_UNAVAILABLE: {
    severity: "error",
    copy: "Live quote unavailable. Refresh market data and retry.",
  },
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
  // PR-07 fix3 — /orders/replace claim
  REPLACEMENT_IN_PROGRESS: {
    severity: "error",
    copy: "Another replacement of this order is in progress or done. Nothing was cancelled — check working orders.",
  },
  ORDER_TARGET_AMBIGUOUS: {
    severity: "error",
    copy: "Could not identify the order to replace. Nothing was cancelled — refresh working orders and retry.",
  },
  // F5 — cancel/modify failure classification
  IB_CONNECTION: { severity: "error", copy: "IB connection lost — retry." },
  // PR-10 — single order owner
  ORDER_NOT_OWNED: {
    severity: "error",
    copy: "This order was not placed by Xenon's order connection. Cancel or modify it in TWS / IBKR Mobile.",
  },
  IB_BROKER_DISCONNECTED: {
    severity: "error",
    copy: "IB is not connected to its servers. Nothing was sent — retry when IB reconnects.",
  },
  ORDERS_HALTED: {
    severity: "error",
    copy: "Order actions are halted after an unexplained IB disconnect. Nothing was sent — the operator must restart the API.",
  },
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
  ORDER_IDENTIFIER_REQUIRED: {
    severity: "error",
    copy: "Modify request must include orderId or permId.",
  },
  PORTFOLIO_SNAPSHOT_REQUIRED: {
    severity: "error",
    copy: "Portfolio snapshot required before placing short-exposure orders.",
  },
  PORTFOLIO_SNAPSHOT_STALE: {
    severity: "error",
    copy: "Portfolio snapshot is stale. Sync the account, then retry.",
  },
  // PR-03b — Gate-4 atomic reservation
  GATE4_UNRESOLVED: {
    severity: "error",
    copy: "Coverage could not be verified. Sync the account and retry.",
  },
  MODIFY_GUARANTEE_MODE: {
    severity: "error",
    copy: "Guarantee mode is fixed at placement. Cancel and re-enter to change it.",
  },
  MODIFY_PENDING: {
    severity: "warn",
    copy: "An earlier change to this order is not confirmed by IB yet. Retry in about two minutes, after IB reports the order again, or cancel the order.",
  },
  COMBO_SPLIT_REQUIRED: {
    severity: "error",
    copy: "Combo legs are checked one by one while netting is off. Send the BUY leg as its own order first; send the SELL leg after the BUY fills and the portfolio syncs.",
  },
  ATTEMPT_BODY_MISMATCH: {
    severity: "error",
    copy: "Retry id was reused with a different order. Rebuild the ticket.",
  },
  // PR-08: same attempt id, different body, and that attempt may be at IB.
  ATTEMPT_ALREADY_PLACED: {
    severity: "warn",
    copy: "This attempt id belongs to an existing order. The requested terms are not confirmed. Check working orders; do not resubmit.",
  },
  ORDERS_PAUSED: {
    severity: "warn",
    copy: "Orders are paused (Admin → Runtime settings): place and modify are refused; cancel still works.",
  },
  READ_ONLY_BROKER: {
    severity: "error",
    copy: "Selected broker is read-only for order placement.",
  },
  // F7 — pending timeout
  PENDING_TIMEOUT: {
    severity: "warn",
    copy: "Order submission timed out before IB acknowledged.",
  },
  // B5 — hard subprocess failure on place (FastAPI runner returned non-2xx).
  SUBPROCESS_ERROR: {
    severity: "error",
    copy: "Order service unreachable — retry in a moment.",
  },
  // IB account scope — new orders never fall back to IB's default account
  ACCOUNT_UNRESOLVED: {
    severity: "error",
    copy: "Broker account unresolved — order not placed.",
  },
  ACCOUNT_NOT_MANAGED: {
    severity: "error",
    copy: "Scope account not managed by this IB login — order not placed.",
  },
  PLACE_OUTCOME_UNKNOWN: {
    severity: "error",
    copy: "Order submitted, status unknown — check working orders. Do not resubmit.",
  },
  FUTU_SEND_UNCERTAIN: {
    severity: "error",
    copy: "Request may have reached Futu — refresh working orders. Do not resend.",
  },
  FUTU_SCOPE_REQUIRED: {
    severity: "error",
    copy: "Futu account environment missing on the request.",
  },
  FUTU_SCOPE_INVALID: {
    severity: "error",
    copy: "Futu account environment is not valid.",
  },
  FUTU_SCOPE_CHANGE_PENDING: {
    severity: "error",
    copy: "Futu account changed with unresolved orders — operator action required.",
  },
  FUTU_ACCOUNT_ABSENT: {
    severity: "error",
    copy: "Futu account not found in OpenD — nothing sent.",
  },
  FUTU_ACCOUNT_AMBIGUOUS: {
    severity: "error",
    copy: "Multiple Futu accounts match — nothing sent.",
  },
  FUTU_ACCOUNT_ENV_MISMATCH: {
    severity: "error",
    copy: "Futu account does not match the selected environment — nothing sent.",
  },
  FUTU_ACCOUNT_NOT_ACTIVE: {
    severity: "error",
    copy: "Futu account is not active — nothing sent.",
  },
  FUTU_ACCOUNT_NO_US_AUTHORITY: {
    severity: "error",
    copy: "Futu account lacks US trading authority — nothing sent.",
  },
  FUTU_OPEND_UNREACHABLE: {
    severity: "error",
    copy: "Futu OpenD is unreachable — retry later.",
  },
  FUTU_NOT_READY: {
    severity: "error",
    copy: "Futu OpenD is not ready — retry later.",
  },
  FUTU_WORKER_BUSY: {
    severity: "error",
    copy: "Futu order worker is busy — retry shortly.",
  },
  FUTU_WORKER_STOPPED: {
    severity: "error",
    copy: "Futu order worker is not running — retry later.",
  },
  FUTU_SEND_WINDOW_CLOSED: {
    severity: "error",
    copy: "Futu modify window closed — nothing sent.",
  },
  FUTU_UNLOCK_SECRET_MISSING: {
    severity: "error",
    copy: "REAL trading unlock is not configured — nothing sent.",
  },
  FUTU_UNLOCK_SECRET_INVALID: {
    severity: "error",
    copy: "REAL trading unlock secret is invalid — nothing sent.",
  },
  FUTU_UNLOCK_FAILED: {
    severity: "error",
    copy: "Futu REAL unlock failed — check operator credentials.",
  },
  FUTU_PRICE_PRECISION: {
    severity: "error",
    copy: "Price or trail value has more decimals than Futu accepts — nothing sent.",
  },
  FUTU_ORDER_TYPE_UNSUPPORTED: {
    severity: "error",
    copy: "This order type is not enabled for this Futu account.",
  },
  FUTU_TIF_UNSUPPORTED: {
    severity: "error",
    copy: "This time in force is not enabled for this order type on Futu.",
  },
  FUTU_SESSION_UNSUPPORTED: {
    severity: "error",
    copy: "This trading session is not enabled for this order type on Futu.",
  },
  FUTU_ORDER_NOT_MODIFIABLE: {
    severity: "warn",
    copy: "Futu modify is not available for this order — cancel it instead.",
  },
  TRIGGER_WOULD_FIRE: {
    severity: "error",
    copy: "Trigger price is on the wrong side of the market — it would fire at once. Nothing sent.",
  },
  MKT_SPREAD_TOO_WIDE: {
    severity: "error",
    copy: "Market order refused: the spread is too wide. Use a limit order.",
  },
  MKT_NOTIONAL_CAP: {
    severity: "error",
    copy: "Market order refused: the order value is above the market-order cap. Use a limit order.",
  },
  FUTU_INVALID_ORDER: {
    severity: "error",
    copy: "Futu rejected the order body — fix inputs and retry.",
  },
  FUTU_REMARK_TOO_LONG: {
    severity: "error",
    copy: "Order remark exceeds Futu length limits.",
  },
  FUTU_NOT_SENT: {
    severity: "error",
    copy: "Futu refused the request before send — retry.",
  },
  FUTU_ORDER_UNVERIFIED: {
    severity: "error",
    copy: "Futu order could not be read before the change — nothing sent. Retry.",
  },
  FUTU_ORDER_NOT_WORKING: {
    severity: "warn",
    copy: "Futu reports this order is no longer working — nothing sent.",
  },
  PROTECT_TP_PARTIAL: {
    severity: "error",
    copy: "The take-profit reached IB and ended; check its fills and close any rest from the order ticket.",
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
