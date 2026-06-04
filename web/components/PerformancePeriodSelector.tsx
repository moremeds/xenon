"use client";

import { PERFORMANCE_PERIODS, type PerformancePeriod } from "@/lib/types";

type Props = {
  value: PerformancePeriod;
  onChange: (next: PerformancePeriod) => void;
};

export default function PerformancePeriodSelector({ value, onChange }: Props) {
  return (
    <div
      className="performance-period-selector"
      role="group"
      aria-label="Performance window"
      data-testid="performance-period-selector"
    >
      {PERFORMANCE_PERIODS.map((p) => {
        const active = p === value;
        return (
          <button
            key={p}
            type="button"
            className={`pill ${active ? "active" : ""}`}
            aria-pressed={active}
            data-testid={`performance-period-${p}`}
            onClick={() => {
              if (!active) onChange(p);
            }}
          >
            {p}
          </button>
        );
      })}
    </div>
  );
}
