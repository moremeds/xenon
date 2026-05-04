export type ProtectionState =
  | "PENDING_ARM"
  | "ARMED"
  | "TRIGGERED"
  | "CLOSED"
  | "CANCELED"
  | "FAILED"
  | "SUPERSEDED";

export interface PositionRule {
  protection_id: number;
  position_key: string;
  rule_kind: "stop_loss" | "trailing_tp" | "take_profit_fixed" | "combo_tp_alert";
  state: ProtectionState;
  asset_class: string;
  config: Record<string, unknown>;
  state_data: Record<string, unknown>;
  position_descriptor: Record<string, unknown>;
  native_order_perm_id: number | null;
  armed_at: string | null;
  triggered_at: string | null;
}

export interface PositionRulesHealth {
  schema_version: 1;
  daemon_alive: boolean;
  market_window: "open" | "closed" | "pre_open" | "post_close";
  next_market_event_at: string;
  last_tick_at: string | null;
  last_tick_age_seconds: number | null;
  rule_counts_by_state: Record<ProtectionState, number>;
  claim_counts_by_status: Record<"PENDING" | "SUBMITTED" | "FILLED" | "FAILED" | "ABANDONED", number>;
  in_flight_claims: number;
  stale_quote_skips_last_hour: number;
  unprotected_position_count: number;
  ib_connected: boolean;
  outbox_dlq_count: number;
}

export async function fetchPositionRules(): Promise<PositionRule[]> {
  const res = await fetch("/api/position-rules");
  if (!res.ok) throw new Error(`fetchPositionRules: ${res.status}`);
  return res.json();
}

export async function fetchHealth(): Promise<PositionRulesHealth> {
  const res = await fetch("/api/position-rules/health");
  if (!res.ok) throw new Error(`fetchHealth: ${res.status}`);
  return res.json();
}

export async function cancelRule(id: number): Promise<{ protection_id: number; state: ProtectionState }> {
  const res = await fetch(`/api/position-rules/${id}/cancel`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.reason_code ?? `cancel failed: ${res.status}`);
  }
  return res.json();
}
