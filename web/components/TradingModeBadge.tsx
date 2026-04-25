"use client";

import { useTradingMode } from "@/lib/useTradingMode";

export default function TradingModeBadge() {
  const mode = useTradingMode();
  if (mode !== "paper") return null;
  return (
    <span
      className="trading-mode-badge"
      title="Backend is connected to the IB paper account"
      aria-label="Paper trading mode"
      style={{
        display: "inline-flex",
        alignItems: "center",
        padding: "2px 10px",
        borderRadius: 999,
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        letterSpacing: "0.08em",
        color: "var(--warning)",
        border: "1px solid var(--warning)",
        background: "transparent",
        textTransform: "uppercase",
        whiteSpace: "nowrap",
      }}
    >
      Paper Mode
    </span>
  );
}
