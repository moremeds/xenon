"use client";

import type { CSSProperties } from "react";
import {
  type FutuAccountEnv,
  setFutuAccountEnv,
  useFutuAccountEnv,
} from "@/lib/futuAccountEnv";

// REAL keeps the warning token so live scope never looks like simulate.
const segmentBtn = (active: boolean, live: boolean): CSSProperties => ({
  fontFamily: "var(--font-mono)",
  fontSize: 11,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  padding: "4px 12px",
  border: "none",
  background: active ? "var(--bg-panel-raised)" : "var(--bg-panel)",
  color: active
    ? live
      ? "var(--warning)"
      : "var(--text-primary)"
    : "var(--text-secondary)",
  cursor: "pointer",
  boxShadow: active
    ? `inset 0 0 0 1px ${live ? "var(--warning)" : "var(--signal-core)"}`
    : "none",
});

/**
 * SIMULATE / REAL scope control, rendered inside the Futu account tab.
 * `onSelect` lets the tab activate itself when an env is picked.
 */
export default function FutuEnvSwitch({
  onSelect,
}: {
  onSelect?: (env: FutuAccountEnv) => void;
}) {
  const env = useFutuAccountEnv();
  const pick = (next: FutuAccountEnv) => {
    setFutuAccountEnv(next);
    onSelect?.(next);
  };

  return (
    <div
      data-testid="futu-env-switch"
      role="group"
      aria-label="Futu account environment"
      style={{
        display: "inline-flex",
        marginTop: 8,
        border: "1px solid var(--border-dim)",
        borderRadius: 4,
        overflow: "hidden",
      }}
    >
      <button
        type="button"
        style={segmentBtn(env === "paper", false)}
        aria-pressed={env === "paper"}
        data-testid="futu-env-simulate"
        onClick={() => pick("paper")}
      >
        SIMULATE
      </button>
      <button
        type="button"
        style={segmentBtn(env === "live", true)}
        aria-pressed={env === "live"}
        data-testid="futu-env-real"
        onClick={() => pick("live")}
      >
        REAL
      </button>
    </div>
  );
}
