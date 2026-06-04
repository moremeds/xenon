"use client";

import type { PerformanceData } from "@/lib/types";

/** Show the user where the displayed numbers come from. When close rows
 *  arrive at 17:30 ET, the underlying NAV series shifts — this subtitle
 *  makes that visible instead of letting the headline silently change.
 *
 *  Pass-3 A4 mitigation: retroactive source change is now visible.
 */
export default function PerformanceFreshness({
  data,
}: {
  data: PerformanceData;
}) {
  if (data.status !== "ok") return null;
  const sources = new Set<string>();
  for (const row of data.series ?? []) {
    if (row.source) sources.add(row.source);
  }
  const sourceLabel =
    sources.size === 0
      ? "no data"
      : sources.size === 1
        ? Array.from(sources)[0]
        : "intraday + close";
  const lastSync = data.last_sync ?? data.as_of ?? "—";
  return (
    <div className="performance-freshness" data-testid="performance-freshness">
      Last refresh {lastSync} · Sources: {sourceLabel}
    </div>
  );
}
