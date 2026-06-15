"use client";

import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

type Props = {
  id: string;
  label: string;
  count?: string;
  children: ReactNode;
};

/**
 * DashboardSection — collapsible wrapper for a dashboard panel.
 * Eyebrow label + optional count chip + chevron. Body hidden when collapsed.
 * Ported from radon DashboardSurface's inner DashboardSection.
 */
export function DashboardSection({ id, label, count, children }: Props) {
  const [open, setOpen] = useState(true);
  return (
    <section
      className={`dashboard-section dashboard-section--${id}`}
      data-testid={`dashboard-section-${id}`}
    >
      <button
        type="button"
        className="dashboard-section__toggle"
        aria-expanded={open}
        aria-controls={`dashboard-section-body-${id}`}
        onClick={() => setOpen((prev) => !prev)}
      >
        <span className="dashboard-section__title">{label}</span>
        <span className="dashboard-section__meta">
          {count ? <span>{count}</span> : null}
          {open ? (
            <ChevronDown size={16} aria-hidden />
          ) : (
            <ChevronRight size={16} aria-hidden />
          )}
        </span>
      </button>
      <div
        id={`dashboard-section-body-${id}`}
        className="dashboard-section__body"
        hidden={!open}
      >
        {children}
      </div>
    </section>
  );
}
