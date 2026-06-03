"use client";

import { Info } from "lucide-react";
import { useState } from "react";

type Summary = {
  total_return: number | null | undefined;
  simple_total_return: number | null | undefined;
  twr_total_return: number | null | undefined;
  irr_total_return: number | null | undefined;
  net_external_flows: number | null | undefined;
};

const DASH = "---";

function fmtPct(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`;
}

function fmtUsd(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  const sign = value >= 0 ? "+" : "-";
  const abs = Math.abs(value);
  return `${sign}$${abs.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

export default function PerformanceHeadlineTooltip({
  summary,
  currency,
}: {
  summary: Summary;
  currency: string;
}) {
  const [open, setOpen] = useState(false);
  // Headline prefers TWR (industry-standard "manager performance" — removes
  // deposit-timing effects), falls back to flow-adjusted simple, then raw
  // total_return. When TWR is null (broker lacks cash-flow data), the simple
  // return is still flow-adjusted vs raw NAV change.
  const headline =
    summary.twr_total_return ??
    summary.simple_total_return ??
    summary.total_return ??
    null;
  const tone =
    headline == null ? "neutral" : headline >= 0 ? "positive" : "negative";
  return (
    <span className="performance-headline-wrapper">
      <span
        className={`performance-headline ${tone}`}
        data-testid="performance-headline"
      >
        {fmtPct(headline)}
      </span>
      <span
        role="button"
        tabIndex={0}
        aria-label="Show return breakdown"
        data-testid="performance-headline-info"
        className="performance-headline-info"
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => {
          if (e.key === "Escape") setOpen(false);
        }}
      >
        <Info size={14} aria-hidden />
      </span>
      {open && (
        <div
          className="performance-headline-tooltip"
          role="tooltip"
          data-testid="performance-headline-tooltip"
        >
          <div className="tooltip-row">
            <span className="tooltip-label">Simple (flow-adj)</span>
            <span className="tooltip-value">
              {fmtPct(summary.simple_total_return)}
            </span>
          </div>
          <div className="tooltip-row">
            <span className="tooltip-label">Time-Weighted (TWR)</span>
            <span className="tooltip-value">
              {fmtPct(summary.twr_total_return)}
            </span>
          </div>
          <div className="tooltip-row">
            <span className="tooltip-label">Money-Weighted (IRR)</span>
            <span className="tooltip-value">
              {fmtPct(summary.irr_total_return)}
            </span>
          </div>
          <div className="tooltip-row tooltip-row-divider">
            <span className="tooltip-label">Net deposits ({currency})</span>
            <span className="tooltip-value">
              {fmtUsd(summary.net_external_flows)}
            </span>
          </div>
        </div>
      )}
    </span>
  );
}
