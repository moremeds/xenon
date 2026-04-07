"use client";

import type { BrokerAccount } from "@/lib/accountContext";
import {
  FUTU_STATUS_CLASS,
  FUTU_STATUS_LABEL,
  type FutuStalenessState,
} from "@/lib/futuStaleness";

export type AccountTabState = {
  label: string; // Display label, e.g. "IB" / "FUTU"
  accountId: string | null;
  environment: string; // "real" / "paper"
  positionCount: number;
  lastSync: string | null;
  netLiquidation: number | null;
  /**
   * 4-state connection/staleness indicator. `live` and `down` cover the
   * IB tab (it's a simple boolean collapsed into the enum); `stale` and
   * `never_synced` are Futu-specific states driven by futuStaleness.ts.
   */
  status: FutuStalenessState;
};

type Props = {
  active: BrokerAccount;
  onChange: (next: BrokerAccount) => void;
  ib: AccountTabState;
  futu: AccountTabState;
};

/**
 * Two-broker tab bar. Click a tab → shell swaps the data source; everything
 * downstream (MetricCards, WorkspaceSections) re-renders against the new
 * portfolio without any component changes.
 *
 * Per plan: layout + information is identical between tabs. The only
 * difference is which portfolio feeds the screens below.
 */
export default function AccountTabBar({ active, onChange, ib, futu }: Props) {
  return (
    <div className="account-tab-bar">
      <AccountTab
        broker="ib"
        state={ib}
        active={active === "ib"}
        onClick={() => onChange("ib")}
      />
      <AccountTab
        broker="futu"
        state={futu}
        active={active === "futu"}
        onClick={() => onChange("futu")}
      />
    </div>
  );
}

function AccountTab({
  broker,
  state,
  active,
  onClick,
}: {
  broker: BrokerAccount;
  state: AccountTabState;
  active: boolean;
  onClick: () => void;
}) {
  const statusLabel = FUTU_STATUS_LABEL[state.status];
  const statusClass = FUTU_STATUS_CLASS[state.status];

  return (
    <button
      type="button"
      className={`account-tab${active ? " account-tab-active" : ""}`}
      onClick={onClick}
      aria-pressed={active}
      aria-label={`Switch to ${state.label} account`}
    >
      <div className="account-tab-header">
        <span className="account-tab-label">ACCOUNT · {state.label}</span>
        <span className={`account-tab-status ${statusClass}`}>● {statusLabel}</span>
      </div>
      <div className="account-tab-id">
        {state.accountId ? truncateId(state.accountId) : "—"}
        {state.environment ? ` · ${state.environment}` : ""}
      </div>
      <div className="account-tab-meta">
        {formatSyncAge(state.lastSync)} · {state.positionCount} pos
      </div>
      {state.netLiquidation != null && (
        <div className="account-tab-metric">net liq {formatCurrency(state.netLiquidation)}</div>
      )}
    </button>
  );
}

// ─── formatting helpers ──────────────────────────────────────────────────

function truncateId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 5)}…${id.slice(-4)}`;
}

function formatSyncAge(iso: string | null): string {
  if (!iso) return "never synced";
  const then = Date.parse(iso);
  if (isNaN(then)) return iso;
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `synced ${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `synced ${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `synced ${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `synced ${days}d ago`;
}

function formatCurrency(n: number): string {
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}
