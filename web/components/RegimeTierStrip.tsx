"use client";

export type RegimeTierLabel =
  | "NORMAL"
  | "EDR"
  | "TIER_2"
  | "TIER_1"
  | "PANIC"
  | "UNKNOWN";

export type RegimeBindingSide = "vcg" | "cri" | "both" | "none";

export type RegimeTierData = {
  vcg_tier: RegimeTierLabel;
  cri_tier: RegimeTierLabel;
  binding_tier: RegimeTierLabel;
  binding_side: RegimeBindingSide;
  vcg_scanned_at: string | null;
  cri_scanned_at: string | null;
  is_stale: boolean;
  panic_active: boolean;
};

const TIER_COLOR: Record<RegimeTierLabel, string> = {
  NORMAL: "var(--positive)",
  EDR: "var(--warning)",
  TIER_2: "var(--warning)",
  TIER_1: "var(--negative)",
  PANIC: "var(--negative)",
  UNKNOWN: "var(--text-muted)",
};

function isBinding(side: RegimeBindingSide, scanner: "vcg" | "cri"): boolean {
  return side === scanner || side === "both";
}

export function RegimeTierStrip({ data }: { data: RegimeTierData | null }) {
  if (!data) return null;

  const vcgBinding = isBinding(data.binding_side, "vcg");
  const criBinding = isBinding(data.binding_side, "cri");

  return (
    <div
      className="regime-tier-strip"
      role="status"
      aria-label="Regime tier status"
    >
      <span
        data-testid="regime-tier-vcg"
        data-binding={vcgBinding}
        className="regime-tier-badge"
        style={{
          color: TIER_COLOR[data.vcg_tier],
          fontWeight: vcgBinding ? 700 : 400,
          textDecoration: vcgBinding ? "underline" : "none",
        }}
      >
        VCG-R: {data.vcg_tier}
      </span>
      <span
        data-testid="regime-tier-cri"
        data-binding={criBinding}
        className="regime-tier-badge"
        style={{
          color: TIER_COLOR[data.cri_tier],
          fontWeight: criBinding ? 700 : 400,
          textDecoration: criBinding ? "underline" : "none",
        }}
      >
        CRI: {data.cri_tier}
      </span>
      {data.panic_active && (
        <span
          data-testid="regime-panic-banner"
          className="regime-panic-banner"
          style={{ color: "var(--negative)", fontWeight: 700 }}
        >
          PANIC — VIX ≥ 48
        </span>
      )}
      {data.is_stale && (
        <span
          data-testid="regime-stale-banner"
          className="regime-stale-banner"
          style={{ color: "var(--text-muted)" }}
        >
          regime data stale — sized conservatively
        </span>
      )}
    </div>
  );
}
