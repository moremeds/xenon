/**
 * Gateway ↔ IB servers state from `/api/health` → `ib_gateway.upstream`
 * (incident 2026-10-26: the API port stayed open while the gateway had lost
 * IB servers). `lost` = the pool saw 1100 / 2110; quotes fail fast with 503.
 */
export type IbUpstreamState = "connected" | "lost" | "unknown";

export type IbUpstream = {
  state: IbUpstreamState;
  since: string | null;
  lastCode: number | null;
};

export const UNKNOWN_UPSTREAM: IbUpstream = {
  state: "unknown",
  since: null,
  lastCode: null,
};

export function parseIbUpstream(health: unknown): IbUpstream {
  const up = (health as { ib_gateway?: { upstream?: unknown } } | null)
    ?.ib_gateway?.upstream as
    | { state?: unknown; since?: unknown; last_code?: unknown }
    | undefined;
  if (!up || typeof up !== "object") return UNKNOWN_UPSTREAM;
  const state: IbUpstreamState =
    up.state === "connected" || up.state === "lost" ? up.state : "unknown";
  return {
    state,
    since: typeof up.since === "string" ? up.since : null,
    lastCode: typeof up.last_code === "number" ? up.last_code : null,
  };
}
