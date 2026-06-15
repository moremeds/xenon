"use client";

import { useEffect, useRef, useState } from "react";

import { isWriterStale } from "@/lib/serviceHealthWindows";
import type { OperatorData } from "@/lib/operatorTypes";
import { useMarketHours } from "@/lib/useMarketHours";

import { BrokersCard } from "./BrokersCard";
import { ReliabilityRollupHeader } from "./ReliabilityRollupHeader";
import { SignalTile } from "./SignalTile";
import { UwQuotaTile } from "./UwQuotaTile";
import { WriterFreshnessTable } from "./WriterFreshnessTable";

const POLL_MS = 8_000;

export default function OperatorConsole() {
  const market = useMarketHours();
  const [data, setData] = useState<OperatorData | null>(null);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [, force] = useState(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch("/api/admin/operator", { cache: "no-store" });
        if (!res.ok) {
          if (alive) setError(`Operator feed error (HTTP ${res.status})`);
          return;
        }
        const json = (await res.json()) as OperatorData;
        if (alive) {
          setData(json);
          setFetchedAt(Date.now());
          setError(null);
        }
      } catch (e) {
        if (alive)
          setError(
            e instanceof Error ? e.message : "Operator feed unreachable",
          );
      }
    };
    load();
    timer.current = setInterval(load, POLL_MS);
    const tick = setInterval(() => force((n) => n + 1), 1_000);
    return () => {
      alive = false;
      if (timer.current) clearInterval(timer.current);
      clearInterval(tick);
    };
  }, []);

  if (!data) {
    return (
      <div className="operator-surface operator-surface--loading">
        {error ? `Operator — ${error}` : "Operator — loading…"}
      </div>
    );
  }

  const updatedSecsAgo = fetchedAt
    ? Math.max(0, Math.round((Date.now() - fetchedAt) / 1000))
    : null;
  // Fresh = recent AND not erroring; a writer that errors every minute is
  // recent but not healthy.
  const freshCount = data.writers.filter(
    (w) => w.state === "ok" && !isWriterStale(w.service, w.age_secs, market),
  ).length;
  const writerSummary = `${freshCount}/${data.writers.length} healthy`;

  const snapTone =
    data.snapshotter.stale_seconds != null &&
    data.snapshotter.stale_seconds > 1800
      ? "fault"
      : "core";
  const orderTone = data.order_submissions.alarm ? "fault" : "neutral";
  const flexTone =
    (data.flex_divergence.divergence_count ?? 0) > 0 ? "warn" : "neutral";

  return (
    <div className="operator-surface">
      <ReliabilityRollupHeader
        verdict={data.ib_auth}
        updatedSecsAgo={updatedSecsAgo}
        writerSummary={writerSummary}
      />
      <BrokersCard data={data} />
      <div className="operator-surface__grid">
        <SignalTile
          label="Snapshotter"
          value={
            data.snapshotter.stale_seconds != null
              ? `${data.snapshotter.stale_seconds}s`
              : "---"
          }
          sub="since last portfolio snapshot"
          tone={snapTone}
        />
        <SignalTile
          label="Order Queue"
          value={data.order_submissions.unknown_count ?? "---"}
          sub={data.order_submissions.alarm ? "ALARM" : "unknown(1h)"}
          tone={orderTone}
        />
        <SignalTile
          label="Flex Divergence"
          value={data.flex_divergence.divergence_count ?? "---"}
          sub={`of ${data.flex_divergence.total_compared ?? "---"}`}
          tone={flexTone}
        />
        <SignalTile
          label="Realtime"
          value={
            data.realtime_subscribers.reachable
              ? data.realtime_subscribers.ib_connected
                ? "live"
                : "ib off"
              : "down"
          }
          sub={`${data.realtime_subscribers.anonymous_count} subs`}
          tone={data.realtime_subscribers.reachable ? "core" : "fault"}
        />
        {/* Live UW quota from x-uw-* headers — self-fetches (30-min RTH auto +
            manual). Replaces the uw_api_stats "no data" tile. */}
        <UwQuotaTile market={market} />
      </div>
      <WriterFreshnessTable writers={data.writers} market={market} />
    </div>
  );
}
