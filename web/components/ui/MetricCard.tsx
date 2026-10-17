"use client";

import type { ReactNode } from "react";
import InfoTooltip from "../InfoTooltip";

/**
 * Shared metric card used by the GEX tab and the UW Analyze detail panel.
 *
 * Visuals are driven by the `.gex-metric-card` / `.gex-metric-label`
 * stack in `web/app/globals.css`. Consumers place one or more of these
 * inside a `.gex-metrics-row` wrapper.
 */
export function MetricCard({
  label,
  value,
  sub,
  color,
  badge,
  tooltip,
}: {
  label: string;
  value: string;
  sub?: ReactNode;
  color?: string;
  badge?: ReactNode;
  tooltip?: string;
}) {
  return (
    <div className="gex-metric-card">
      <div
        className="gex-metric-label"
        style={{ display: "flex", alignItems: "center", gap: 4 }}
      >
        {label}
        {tooltip && <InfoTooltip text={tooltip} />}
        {badge}
      </div>
      <div
        className="gex-metric-value"
        style={{ color: color || "var(--text-primary)" }}
      >
        {value}
      </div>
      {sub != null && sub !== "" && <div className="gex-metric-sub">{sub}</div>}
    </div>
  );
}

/**
 * Small colour-coded badge tagging the provenance of a metric.
 * Kept here so both `GexPanel` and the UW Analyze detail panel can share
 * the same visual without a re-export intermediate.
 */
export function SourceBadge({ source }: { source: "uw" | "mq" | "both" }) {
  const styles: Record<string, React.CSSProperties> = {
    uw: {
      background: "color-mix(in srgb, var(--signal-deep) 18%, transparent)",
      color: "var(--signal-core)",
      border: "0.5px solid color-mix(in srgb, var(--signal-deep) 40%, transparent)",
    },
    mq: {
      background: "color-mix(in srgb, var(--info) 15%, transparent)",
      color: "var(--info)",
      border: "0.5px solid color-mix(in srgb, var(--info) 35%, transparent)",
    },
    both: {
      background: "color-mix(in srgb, var(--signal-strong) 12%, transparent)",
      color: "var(--signal-core)",
      border: "0.5px solid color-mix(in srgb, var(--signal-strong) 30%, transparent)",
    },
  };
  const labels = { uw: "UW", mq: "MQ", both: "UW+MQ" };
  return (
    <span
      style={{
        ...styles[source],
        fontSize: 9,
        fontWeight: 500,
        padding: "1px 5px",
        borderRadius: 2,
        letterSpacing: "0.06em",
      }}
    >
      {labels[source]}
    </span>
  );
}
