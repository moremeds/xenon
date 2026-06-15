import { MarketState } from "@/lib/useMarketHours";

export type StalenessWindow = {
  open: number | null;
  extended: number | null;
  closed: number | null;
};

// Max age (seconds) before a writer is "stale", per market state.
// null = not expected to run in that state → never stale.
export const SERVICE_WINDOWS: Record<string, StalenessWindow> = {
  ib_activity_poller: { open: 180, extended: 300, closed: 900 },
  ib_fills_replay: { open: null, extended: null, closed: null },
  ib_rehydrate: { open: null, extended: null, closed: null },
  futu_history: { open: null, extended: null, closed: null },
  naked_short_audit: { open: 3600, extended: 3600, closed: null },
};

const DEFAULT_WINDOW: StalenessWindow = {
  open: 300,
  extended: 600,
  closed: null,
};

export function isWriterStale(
  service: string,
  ageSecs: number | null,
  market: MarketState,
): boolean {
  if (ageSecs == null) return true; // never reported / missing
  const w = SERVICE_WINDOWS[service] ?? DEFAULT_WINDOW;
  const limit =
    market === MarketState.OPEN
      ? w.open
      : market === MarketState.EXTENDED
        ? w.extended
        : w.closed;
  if (limit == null) return false; // not expected to run in this window
  return ageSecs > limit;
}
