"use client";

import { useEffect, useState } from "react";

import { fetchHealth, type PositionRulesHealth } from "@/lib/api/positionRules";

type HealthClass = "red" | "amber" | "green";

const HEALTH_TONE: Record<HealthClass, string> = {
  red: "bg-[var(--fault)] text-[var(--bg-base)]",
  amber: "bg-[var(--warning)] text-[var(--bg-base)]",
  green: "bg-[var(--signal-core)] text-[var(--bg-base)]",
};

function count(map: Record<string, number>, key: string): number {
  return map[key] ?? 0;
}

export function classifyPositionRulesHealth(health: PositionRulesHealth): HealthClass {
  if (!health.daemon_alive) return "red";
  if (health.outbox_dlq_count > 0) return "red";
  if (count(health.claim_counts_by_status, "FAILED") > 0) return "red";
  if (health.market_window === "open" && (health.last_tick_age_seconds ?? 0) > 300) return "red";

  if (count(health.rule_counts_by_state, "FAILED") > 0) return "amber";
  if (health.unprotected_position_count > 0) return "amber";
  if (health.in_flight_claims > 0) return "amber";
  if (health.stale_quote_skips_last_hour > 5) return "amber";
  if (!health.ib_connected && health.market_window === "open") return "amber";
  return "green";
}

function healthTitle(health: PositionRulesHealth): string {
  if (health.market_window === "open") {
    return `Market open. Last tick ${health.last_tick_age_seconds ?? 0}s ago.`;
  }
  return `Market ${health.market_window}. Synthetic monitor resumes at ${health.next_market_event_at}; native brackets remain armed.`;
}

export function GlobalHealthIndicator() {
  const [health, setHealth] = useState<PositionRulesHealth | null>(null);

  useEffect(() => {
    let active = true;

    async function tick() {
      try {
        const next = await fetchHealth();
        if (active) setHealth(next);
      } catch {
        if (active) setHealth(null);
      }
    }

    void tick();
    const timer = setInterval(tick, 30_000);

    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  if (health === null) {
    return (
      <div className="inline-flex items-center rounded-full border border-[var(--border-dim)] px-2 py-1 font-mono text-[11px] text-[var(--text-secondary)]">
        PR health
      </div>
    );
  }

  const cls = classifyPositionRulesHealth(health);
  const armed = count(health.rule_counts_by_state, "ARMED");

  return (
    <div
      className={`inline-flex items-center gap-1 rounded-full px-2 py-1 font-mono text-[11px] leading-none ${HEALTH_TONE[cls]}`}
      title={healthTitle(health)}
      data-cls={cls}
    >
      <span>PR</span>
      <span>{armed} armed</span>
    </div>
  );
}
