"use client";

import Link from "next/link";
import type { WorkspaceSection } from "@/lib/types";
import { navItems } from "@/lib/data";
import {
  classifySubscriber,
  formatAge,
  DOT_CLASS,
  type SubscriberHealth,
} from "@/lib/subscriberHealth";
import type { IbUpstream } from "@/lib/ibUpstream";

type SidebarProps = {
  activeSection: WorkspaceSection;
  actionTone: string;
  ibConnected?: boolean;
  /** Gateway ↔ IB servers (`/health` ib_gateway.upstream). */
  ibUpstream?: IbUpstream;
  lastSync?: string | null;
  subscribers?: SubscriberHealth[];
  subscribersReachable?: boolean;
  anonymousCount?: number;
};

export default function Sidebar({
  activeSection,
  actionTone,
  ibConnected = true,
  ibUpstream,
  lastSync,
  subscribers = [],
  subscribersReachable = false,
  anonymousCount = 0,
}: SidebarProps) {
  const syncTime = lastSync ? new Date(lastSync).toLocaleTimeString() : "—";
  const appVersion = process.env.NEXT_PUBLIC_APP_VERSION;
  const upstreamLost = ibUpstream?.state === "lost";

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
                  <Icon
                    size={14}
                    style={{ color: actionTone }}
                    strokeWidth={2}
                  />
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
              className={`status-dot ${!ibConnected ? "status-dot-dead" : upstreamLost ? "status-dot-stale" : "status-dot-live"}`}
            />
            {!ibConnected
              ? "OFFLINE"
              : upstreamLost
                ? "NO IB SERVERS"
                : "CONNECTED"}
          </span>
        </div>
        {ibConnected && upstreamLost && (
          <div className="status-row" data-testid="ib-upstream-lost">
            <span className="status-muted">
              {`Gateway lost IB servers${ibUpstream?.lastCode != null ? ` (${ibUpstream.lastCode})` : ""}`}
            </span>
            <span>
              {ibUpstream?.since
                ? new Date(ibUpstream.since).toLocaleTimeString()
                : "—"}
            </span>
          </div>
        )}
        <div className="status-row">
          <span>Last Sync</span>
          <span>{syncTime}</span>
        </div>
        <div className="status-row">
          <span>Source</span>
          <span>IB Gateway</span>
        </div>
        <div className="status-row">
          <span>Version</span>
          <span>{appVersion ? `v${appVersion}` : "—"}</span>
        </div>

        <div className="status-row status-row-header">
          <span>Subscribers</span>
          <span />
        </div>
        {!subscribersReachable ? (
          <div className="status-row">
            <span className="status-muted">stream offline</span>
          </div>
        ) : subscribers.length === 0 ? (
          <div className="status-row">
            <span className="status-muted">none</span>
          </div>
        ) : (
          subscribers.map((s) => {
            const liveness = classifySubscriber(s);
            const age =
              liveness === "offline"
                ? `offline ${formatAge(s.offlineForMs ?? s.lastSeenMsAgo)}`
                : formatAge(s.lastPongMsAgo);
            return (
              <div className="status-row" key={s.id}>
                <span className="status-sub-id">{s.id}</span>
                <span className="status-dot-wrap">
                  <span className={`status-dot ${DOT_CLASS[liveness]}`} />
                  {age}
                </span>
              </div>
            );
          })
        )}
        {anonymousCount > 0 && (
          <div className="status-row">
            <span className="status-muted">+{anonymousCount} app clients</span>
          </div>
        )}
      </div>
    </aside>
  );
}
