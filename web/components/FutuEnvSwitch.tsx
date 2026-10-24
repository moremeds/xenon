"use client";

import type { CSSProperties } from "react";
import {
  type FutuAccountEnv,
  setFutuAccountEnv,
  useFutuAccountEnv,
} from "@/lib/futuAccountEnv";

const badgeBase: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  padding: "2px 10px",
  borderRadius: 999,
  fontFamily: "var(--font-mono)",
  fontSize: 11,
  letterSpacing: "0.08em",
  background: "transparent",
  textTransform: "uppercase",
  whiteSpace: "nowrap",
  marginLeft: 8,
};

export function FutuAccountEnvBadge({ env }: { env: FutuAccountEnv }) {
  const isLive = env === "live";
  const style: CSSProperties = {
    ...badgeBase,
    color: isLive ? "var(--warning)" : "var(--neutral)",
    border: isLive ? "1px solid var(--warning)" : "1px solid var(--neutral)",
  };
  return (
    <span
      className="futu-account-env-badge"
      style={style}
      data-testid="futu-env-badge"
      aria-label={isLive ? "Futu real account" : "Futu simulate account"}
    >
      {isLive ? "REAL" : "SIMULATE"}
    </span>
  );
}

/**
 * SIMULATE / REAL scope control — visible only on the Futu account tab.
 */
const segmentBtn = (active: boolean): CSSProperties => ({
  fontFamily: "var(--font-mono)",
  fontSize: 11,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  padding: "6px 14px",
  border: "none",
  background: active ? "var(--bg-panel-raised)" : "var(--bg-panel)",
  color: active ? "var(--text-primary)" : "var(--text-secondary)",
  cursor: "pointer",
  boxShadow: active ? "inset 0 0 0 1px var(--signal-core)" : "none",
});

export default function FutuEnvSwitch() {
  const env = useFutuAccountEnv();

  return (
    <div
      data-testid="futu-env-switch"
      role="group"
      aria-label="Futu account environment"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        marginBottom: 16,
        padding: "10px 12px",
        border: "1px solid var(--border-dim)",
        borderRadius: 4,
        background: "var(--bg-panel)",
      }}
    >
      <span
        style={{
          fontSize: 11,
          letterSpacing: "0.06em",
          textTransform: "uppercase",
          color: "var(--text-secondary)",
        }}
      >
        Futu environment
      </span>
      <div
        style={{
          display: "inline-flex",
          border: "1px solid var(--border-dim)",
          borderRadius: 4,
          overflow: "hidden",
        }}
      >
        <button
          type="button"
          style={segmentBtn(env === "paper")}
          aria-pressed={env === "paper"}
          data-testid="futu-env-simulate"
          onClick={() => setFutuAccountEnv("paper")}
        >
          SIMULATE
        </button>
        <button
          type="button"
          style={segmentBtn(env === "live")}
          aria-pressed={env === "live"}
          data-testid="futu-env-real"
          onClick={() => setFutuAccountEnv("live")}
        >
          REAL
        </button>
      </div>
    </div>
  );
}
