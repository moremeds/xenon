"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { UwQuota } from "@/lib/operatorTypes";
import { MarketState } from "@/lib/useMarketHours";

// Auto-refresh cadence: every 30 min, RTH-only (the daily quota is the signal;
// per-minute counter resets too fast to chase). A manual button covers ad-hoc
// readings any time. Each fetch consumes one UW hit.
const REFRESH_MS = 30 * 60 * 1000;

function fmtK(n: number | null): string {
  if (n == null) return "—";
  if (n >= 10000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function tone(q: UwQuota | null): "core" | "warn" | "fault" | "neutral" {
  if (!q || !q.configured) return "neutral";
  if (q.error) return "fault";
  if (q.daily_count != null && q.daily_limit) {
    const ratio = q.daily_count / q.daily_limit;
    if (ratio > 0.9) return "fault";
    if (ratio > 0.75) return "warn";
    return "core";
  }
  return "neutral";
}

export function UwQuotaTile({ market }: { market: MarketState }) {
  const [q, setQ] = useState<UwQuota | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/admin/uw-quota", { cache: "no-store" });
      if (res.ok) setQ((await res.json()) as UwQuota);
    } catch {
      /* keep last good reading */
    } finally {
      setLoading(false);
    }
  }, []);

  // Read latest market state inside the interval without resetting it.
  const marketRef = useRef(market);
  marketRef.current = market;

  useEffect(() => {
    load(); // one reading on mount
    const id = setInterval(() => {
      if (marketRef.current === MarketState.OPEN) load(); // 30-min, RTH only
    }, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  const value = !q
    ? "…"
    : !q.configured
      ? "not configured"
      : q.error
        ? "error"
        : `${fmtK(q.daily_count)} / ${fmtK(q.daily_limit)}`;

  const sub =
    !q || !q.configured
      ? "UW quota"
      : q.error
        ? q.error
        : q.minute_remaining != null
          ? `${q.minute_remaining}/min left`
          : "daily used / limit";

  return (
    <div className="operator-tile">
      <span className="operator-tile__label operator-tile__label--row">
        <span>UW API</span>
        <button
          type="button"
          className="operator-tile__refresh"
          onClick={load}
          disabled={loading}
          title="Refresh UW quota (consumes one UW hit)"
          aria-label="Refresh UW quota"
        >
          ⟳
        </button>
      </span>
      <span className={`operator-tile__value operator-tile__value--${tone(q)}`}>
        {value}
      </span>
      <span className="operator-tile__sub">{sub}</span>
    </div>
  );
}
