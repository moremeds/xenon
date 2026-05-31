"use client";

import Link from "next/link";
import type { WorkspaceSection } from "@/lib/types";
import { navItems } from "@/lib/data";
import { useUwStats } from "@/lib/useUwStats";
import { GlobalHealthIndicator } from "@/components/portfolio/GlobalHealthIndicator";

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

  const daily = uwStats?.daily;
  const latency = uwStats?.latency_ms;
  const uwActive = daily !== undefined && daily.requests > 0;
  const hasIssues =
    daily !== undefined && (daily.requests_4xx > 0 || daily.requests_5xx > 0);

  const cacheHitPct = daily?.cache_hit_pct ?? null;

  const count2xx = daily?.requests_2xx ?? null;
  const count4xx = daily?.requests_4xx ?? null;
  const count5xx = daily?.requests_5xx ?? null;

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
        <div className="status-row">
          <span>Protection</span>
          <GlobalHealthIndicator />
        </div>

        <div className="sidebar-footer-divider" />

        <div className="status-row">
          <span>UW Today</span>
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
            {daily ? formatCount(daily.requests) : "—"}
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
        {daily && (daily.requests_4xx > 0 || daily.requests_5xx > 0) ? (
          <div className="status-row">
            <span>Errors</span>
            <span className="uw-stats-errors">
              {daily.requests_4xx > 0 ? `${daily.requests_4xx} 4xx` : null}
              {daily.requests_4xx > 0 && daily.requests_5xx > 0 ? " / " : null}
              {daily.requests_5xx > 0 ? `${daily.requests_5xx} 5xx` : null}
            </span>
          </div>
        ) : null}
      </div>
    </aside>
  );
}
