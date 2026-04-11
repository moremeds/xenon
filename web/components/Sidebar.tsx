"use client";

import Link from "next/link";
import type { WorkspaceSection } from "@/lib/types";
import { navItems } from "@/lib/data";
import { useUwStats } from "@/lib/useUwStats";

type SidebarProps = {
  activeSection: WorkspaceSection;
  actionTone: string;
  ibConnected?: boolean;
  lastSync?: string | null;
};

function formatCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export default function Sidebar({
  activeSection,
  actionTone,
  ibConnected = true,
  lastSync,
}: SidebarProps) {
  const syncTime = lastSync ? new Date(lastSync).toLocaleTimeString() : "—";
  const uwStats = useUwStats();

  const totals = uwStats?.totals;
  const latency = uwStats?.latency_ms;
  const uwActive = totals !== undefined && totals.requests > 0;
  const hasIssues =
    totals !== undefined && (totals.rate_limits > 0 || totals.failures > 0);

  const cacheHitPct =
    totals && totals.requests > 0
      ? Math.round((totals.cached / totals.requests) * 100)
      : null;

  // Aggregate raw HTTP status → 2xx / 4xx / 5xx classes. Connection
  // errors are bucketed with 5xx (the collector records them with no
  // status, so they only appear in totals.connection_errors).
  const byStatus = uwStats?.by_status;
  let count2xx: number | null = null;
  let count4xx: number | null = null;
  let count5xx: number | null = null;
  if (byStatus) {
    count2xx = 0;
    count4xx = 0;
    count5xx = 0;
    for (const [codeStr, n] of Object.entries(byStatus)) {
      const code = Number(codeStr);
      if (code >= 200 && code < 300) count2xx += n;
      else if (code >= 400 && code < 500) count4xx += n;
      else if (code >= 500 && code < 600) count5xx += n;
    }
    if (totals?.connection_errors) count5xx += totals.connection_errors;
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="logo-icon" />
        <span className="logo-text">Xenon</span>
      </div>

      <nav className="sidebar-nav">
        {navItems
          .filter((item) => !item.hidden)
          .map((item) => {
            const Icon = item.icon;
            return (
              <Link
                key={item.label}
                href={item.href}
                className={
                  item.route === activeSection ? "nav-item active" : "nav-item"
                }
              >
                <span className="nav-icon">
                  <Icon size={14} color={actionTone} strokeWidth={2} />
                </span>
                {item.label}
              </Link>
            );
          })}
      </nav>

      <div className="sidebar-footer">
        <div className="status-row">
          <span>IB Gateway</span>
          <span className="status-dot-wrap">
            <span
              className={`status-dot ${ibConnected ? "status-dot-live" : "status-dot-dead"}`}
            />
            {ibConnected ? "CONNECTED" : "OFFLINE"}
          </span>
        </div>
        <div className="status-row">
          <span>Last Sync</span>
          <span>{syncTime}</span>
        </div>
        <div className="status-row">
          <span>Source</span>
          <span>IB Gateway</span>
        </div>

        <div className="sidebar-footer-divider" />

        <div className="status-row">
          <span>UW API</span>
          <span className="status-dot-wrap">
            <span
              className={`status-dot ${
                hasIssues
                  ? "status-dot-dead"
                  : uwActive
                    ? "status-dot-live"
                    : ""
              }`}
            />
            {totals ? formatCount(totals.requests) : "—"}
          </span>
        </div>
        <div className="status-row">
          <span>Cache Hit</span>
          <span>{cacheHitPct !== null ? `${cacheHitPct}%` : "—"}</span>
        </div>
        <div className="status-row">
          <span>Latency p95</span>
          <span>
            {latency?.p95 !== undefined ? `${Math.round(latency.p95)}ms` : "—"}
          </span>
        </div>
        <div className="status-row">
          <span>2xx</span>
          <span style={{ color: count2xx ? "var(--positive)" : undefined }}>
            {count2xx !== null ? formatCount(count2xx) : "—"}
          </span>
        </div>
        <div className="status-row">
          <span>4xx</span>
          <span style={{ color: count4xx ? "var(--warning)" : undefined }}>
            {count4xx !== null ? formatCount(count4xx) : "—"}
          </span>
        </div>
        <div className="status-row">
          <span>5xx</span>
          <span style={{ color: count5xx ? "var(--negative)" : undefined }}>
            {count5xx !== null ? formatCount(count5xx) : "—"}
          </span>
        </div>
        {totals && (totals.rate_limits > 0 || totals.failures > 0) ? (
          <div className="status-row">
            <span>Errors</span>
            <span className="uw-stats-errors">
              {totals.rate_limits > 0 ? `${totals.rate_limits} 429` : null}
              {totals.rate_limits > 0 && totals.failures > totals.rate_limits
                ? " / "
                : null}
              {totals.failures > totals.rate_limits
                ? `${totals.failures - totals.rate_limits} err`
                : null}
            </span>
          </div>
        ) : null}
      </div>
    </aside>
  );
}
