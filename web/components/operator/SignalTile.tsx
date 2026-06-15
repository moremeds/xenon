import type { ReactNode } from "react";

export type SignalTone = "core" | "fault" | "warn" | "neutral";

export function SignalTile({
  label,
  value,
  sub,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: SignalTone;
}) {
  return (
    <div className="operator-tile">
      <span className="operator-tile__label">{label}</span>
      <span className={`operator-tile__value operator-tile__value--${tone}`}>
        {value}
      </span>
      {sub != null ? <span className="operator-tile__sub">{sub}</span> : null}
    </div>
  );
}
