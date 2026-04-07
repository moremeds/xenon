/**
 * Futu tab staleness state machine.
 *
 * Unlike criStaleness/vcgStaleness (which return a plain boolean), Futu
 * needs a 4-way enum because the AccountTabBar distinguishes LIVE, STALE,
 * NEVER SYNCED, and DOWN — each with its own color and label.
 *
 * State precedence (first match wins):
 *   1. never_synced — hook's neverSynced flag is true, or no envelope AND no error
 *   2. down         — hook has an error AND no fallback envelope
 *   3. stale        — envelope.is_stale is true (the Next.js proxy sets this
 *                     when FastAPI is unreachable and the disk-cache fallback
 *                     kicked in — degraded-but-usable, NOT down), OR market is
 *                     open AND fetched_at is more than 60s old
 *   4. live         — everything else
 */

import type { FutuPortfolioEnvelope } from "@/lib/futuPortfolioAdapter";

export type FutuStalenessState = "live" | "stale" | "never_synced" | "down";

export type ComputeFutuStalenessArgs = {
  envelope: FutuPortfolioEnvelope | null;
  error: string | null;
  neverSynced: boolean;
  marketOpen: boolean;
  now?: number;          // injectable for tests
  staleAfterMs?: number; // default 60_000
};

export function computeFutuStaleness({
  envelope,
  error,
  neverSynced,
  marketOpen,
  now = Date.now(),
  staleAfterMs = 60_000,
}: ComputeFutuStalenessArgs): FutuStalenessState {
  if (neverSynced) return "never_synced";

  if (envelope == null) {
    return error ? "down" : "never_synced";
  }

  if (envelope.is_stale) return "stale";

  if (marketOpen && envelope.fetched_at) {
    const ageMs = now - Date.parse(envelope.fetched_at);
    if (!Number.isNaN(ageMs) && ageMs > staleAfterMs) return "stale";
  }

  return "live";
}

export const FUTU_STATUS_LABEL: Record<FutuStalenessState, string> = {
  live: "LIVE",
  stale: "STALE",
  never_synced: "NEVER SYNCED",
  down: "DOWN",
};

export const FUTU_STATUS_CLASS: Record<FutuStalenessState, string> = {
  live: "tab-status-live",
  stale: "tab-status-stale",
  never_synced: "tab-status-never",
  down: "tab-status-down",
};
