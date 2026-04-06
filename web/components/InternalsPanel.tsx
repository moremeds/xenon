"use client";

import { useMemo, useState } from "react";
import { useRegime } from "@/lib/useRegime";
import { MarketState } from "@/lib/useMarketHours";
import { RegimeStrip, RegimeStripCell } from "@/components/RegimeStrip";
import InternalsSkewChart from "@/components/InternalsSkewChart";

type SpxSkewHistoryPoint = { date: string; spx_skew?: number | null; value?: number | null };
type TimeframeWindow = "6M" | "1Y" | "2Y" | "5Y" | "ALL";

function fmtSigned(v: number | null | undefined, decimals = 4): string {
  if (v == null || !Number.isFinite(v)) return "---";
  return `${v >= 0 ? "+" : ""}${v.toFixed(decimals)}`;
}

function subtractWindow(date: Date, window: TimeframeWindow): Date {
  const out = new Date(date.getTime());
  if (window === "6M") out.setMonth(out.getMonth() - 6);
  if (window === "1Y") out.setFullYear(out.getFullYear() - 1);
  if (window === "2Y") out.setFullYear(out.getFullYear() - 2);
  if (window === "5Y") out.setFullYear(out.getFullYear() - 5);
  return out;
}

const TIMEFRAME_OPTIONS: ReadonlyArray<{ key: TimeframeWindow; label: string }> = [
  { key: "6M", label: "6M" },
  { key: "1Y", label: "1Y" },
  { key: "2Y", label: "2Y" },
  { key: "5Y", label: "5Y" },
  { key: "ALL", label: "ALL" },
];

export default function InternalsPanel({ marketState }: { marketState?: MarketState }) {
  const { data, loading, lastSync, syncing } = useRegime(marketState, { endpoint: "/api/internals" });
  const [timeframe, setTimeframe] = useState<TimeframeWindow>("ALL");

  const nqSkewHistory = useMemo(
    () =>
      (data?.nq_skew_history ?? [])
        .map((entry) => ({ date: entry.date, value: entry.nq_skew }))
        .filter((entry): entry is { date: string; value: number } =>
          Number.isFinite(entry.value) && entry.value !== null && entry.value !== undefined,
        ),
    [data?.nq_skew_history],
  );
  const spxSkewHistory = useMemo(
    () =>
      ((data?.spx_skew_history ?? data?.nq_skew_history ?? []) as SpxSkewHistoryPoint[])
        .map((entry) => ({ date: entry.date, value: entry.spx_skew ?? entry.value }))
        .filter((entry): entry is { date: string; value: number } =>
          Number.isFinite(entry.value) && entry.value !== null && entry.value !== undefined,
        ),
    [data?.spx_skew_history, data?.nq_skew_history],
  );
  const latestDate = useMemo(() => {
    let maxTs = -Infinity;
    for (const p of [...nqSkewHistory, ...spxSkewHistory]) {
      const ts = new Date(p.date).getTime();
      if (Number.isFinite(ts)) {
        maxTs = Math.max(maxTs, ts);
      }
    }
    return Number.isFinite(maxTs) ? new Date(maxTs) : null;
  }, [nqSkewHistory, spxSkewHistory]);
  const filteredNqSkewHistory = useMemo(() => {
    if (!latestDate || timeframe === "ALL") return nqSkewHistory;
    const cutoff = subtractWindow(latestDate, timeframe);
    return nqSkewHistory.filter((entry) => new Date(entry.date) >= cutoff);
  }, [latestDate, nqSkewHistory, timeframe]);
  const filteredSpxSkewHistory = useMemo(() => {
    if (!latestDate || timeframe === "ALL") return spxSkewHistory;
    const cutoff = subtractWindow(latestDate, timeframe);
    return spxSkewHistory.filter((entry) => new Date(entry.date) >= cutoff);
  }, [latestDate, spxSkewHistory, timeframe]);
  const latestSpxSkew = filteredSpxSkewHistory.length > 0
    ? filteredSpxSkewHistory[filteredSpxSkewHistory.length - 1]?.value
    : null;
  const latestNqSkew = filteredNqSkewHistory.length > 0
    ? filteredNqSkewHistory[filteredNqSkewHistory.length - 1]?.value
    : null;

  if (loading && !data) {
    return (
      <div className="regime-panel">
        <div className="regime-empty">Loading internals...</div>
      </div>
    );
  }

  if (!data && !syncing) {
    return (
      <div className="regime-panel">
        <div className="regime-empty">No internals data available.</div>
      </div>
    );
  }

  return (
    <div className="regime-panel">
      <div className="internals-timeframe-switch">
        <span className="internals-timeframe-label">TIME WINDOW</span>
        <div className="internals-timeframe-toggle">
          {TIMEFRAME_OPTIONS.map((option) => (
            <button
              key={option.key}
              type="button"
              className={`internals-timeframe-btn ${timeframe === option.key ? "active" : ""}`}
              onClick={() => setTimeframe(option.key)}
              aria-pressed={timeframe === option.key}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <RegimeStrip>
        <RegimeStripCell
          testId="strip-internals-nq-skew"
          label="NQ SKEW"
          value={fmtSigned(latestNqSkew)}
          sub="NQ - SPX"
          change={null}
        />
        <RegimeStripCell
          testId="strip-internals-spx-skew"
          label="S&P 500 SKEW"
          value={fmtSigned(latestSpxSkew)}
          sub="E-Mini S&P 500"
          change={null}
        />
      </RegimeStrip>
      <div className="regime-strip-cell regime-empty" style={{ justifyContent: "center" }}>
        <div className="regime-strip-sub">Window: {timeframe}</div>
      </div>

      <InternalsSkewChart
        history={filteredNqSkewHistory}
        title="NQ SKEW HISTORY"
        seriesLabel="NQ - SPX"
        dataTestId="internals-nq-skew-chart"
        lineColor="var(--signal-core)"
      />
      <InternalsSkewChart
        history={filteredSpxSkewHistory}
        title="S&P 500 SKEW HISTORY"
        seriesLabel="S&P 500"
        dataTestId="internals-spx-skew-chart"
        lineColor="var(--positive)"
      />
      <div className="regime-strip-cell regime-empty" style={{ justifyContent: "center" }}>
        <div className="regime-strip-sub">
          {lastSync ? `Last scan: ${new Date(lastSync).toLocaleTimeString()}` : "No scan time"}
        </div>
      </div>
    </div>
  );
}
