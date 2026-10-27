import type { IbAuthVerdict, IbPoolRole } from "@/lib/operatorTypes";
import { MarketState } from "@/lib/useMarketHours";

// Snapshots only land while IB is syncing; overnight silence is expected, so a
// stale snapshotter is a fault only when the market is OPEN or EXTENDED.
const SNAPSHOT_STALE_SECS = 1800;

export function snapshotterTone(
  staleSeconds: number | null,
  market: MarketState,
): "core" | "fault" | "neutral" {
  if (staleSeconds == null || staleSeconds <= SNAPSHOT_STALE_SECS)
    return "core";
  return market === MarketState.CLOSED ? "neutral" : "fault";
}

// A gateway can report `authenticated` while a pool role (sync / orders / data)
// has lost its connection. Name those roles; empty unless authenticated.
export function downPoolRoles(
  verdict: IbAuthVerdict,
  pool: Record<string, IbPoolRole>,
): string[] {
  if (verdict !== "authenticated") return [];
  return Object.entries(pool)
    .filter(([, info]) => !info?.connected)
    .map(([role]) => role);
}
