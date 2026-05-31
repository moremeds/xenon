import type { ProtectionState } from "@/lib/api/positionRules";

export type ShieldState = ProtectionState | "NONE" | "UNCLASSIFIED";

export const SHIELD_BADGE_TONE_BY_STATE: Record<ShieldState, string> = {
  ARMED: "bg-[var(--signal-core)] text-[var(--bg-base)]",
  PENDING_ARM: "bg-[var(--warning)] text-[var(--bg-base)]",
  TRIGGERED: "bg-[var(--dislocation)] text-[var(--bg-base)]",
  FAILED: "bg-[var(--fault)] text-[var(--bg-base)]",
  CANCELED: "bg-[var(--neutral)] text-[var(--bg-base)]",
  CLOSED: "bg-[var(--neutral)] text-[var(--bg-base)]",
  SUPERSEDED: "bg-[var(--neutral)] text-[var(--bg-base)]",
  NONE: "border border-[var(--border-dim)] bg-[var(--bg-panel-raised)] text-[var(--text-secondary)]",
  UNCLASSIFIED: "bg-[var(--neutral)] text-[var(--bg-base)]",
};

interface ShieldBadgeProps {
  state: ShieldState;
  count?: number;
  onClick?: () => void;
  ariaLabel?: string;
}

export function ShieldBadge({ state, count, onClick, ariaLabel }: ShieldBadgeProps) {
  const tone = SHIELD_BADGE_TONE_BY_STATE[state];

  return (
    <button
      type="button"
      aria-label={ariaLabel ?? `Protection ${state}`}
      onClick={onClick}
      className={`inline-flex h-6 min-w-6 items-center justify-center gap-1 rounded-full px-2 font-mono text-[11px] leading-none ${tone}`}
      data-state={state}
      data-tone={tone}
    >
      <span>PR</span>
      {count !== undefined ? <span>{count}</span> : null}
    </button>
  );
}
