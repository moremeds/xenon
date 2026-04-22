"use client";

import { useState } from "react";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import Modal from "./Modal";

type Preset = "close" | "trailing_sl" | "trailing_tp" | "roll";

const PRESETS: ReadonlyArray<{
  id: Preset;
  label: string;
  disabled: boolean;
  tooltip?: string;
}> = [
  { id: "close", label: "Close", disabled: false },
  {
    id: "trailing_sl",
    label: "Trailing Stop Loss",
    disabled: true,
    tooltip: "Coming soon — requires TRAIL order support",
  },
  {
    id: "trailing_tp",
    label: "Trailing Take Profit",
    disabled: true,
    tooltip: "Coming soon — requires TRAIL order support",
  },
  {
    id: "roll",
    label: "Roll",
    disabled: true,
    tooltip: "Coming soon — restructuring ticket in follow-up spec",
  },
];

type Props = {
  position: PortfolioPosition;
  prices: Record<string, PriceData>;
  onClose: () => void;
  onSubmitted?: (orderId: string) => void;
};

export default function PositionOrderModal({
  position,
  prices,
  onClose,
  onSubmitted,
}: Props) {
  const [active, setActive] = useState<Preset>("close");

  return (
    <Modal
      open={true}
      onClose={onClose}
      title={`Order — ${position.ticker} ${position.structure}`}
    >
      <div
        className="position-order-preset-bar"
        role="group"
        aria-label="Order presets"
      >
        {PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => !p.disabled && setActive(p.id)}
            disabled={p.disabled}
            aria-pressed={active === p.id}
            title={p.tooltip}
            className={`preset-tile ${active === p.id ? "active" : ""}`}
          >
            {p.label}
          </button>
        ))}
      </div>
      {active === "close" && (
        <div data-testid="close-preset-panel">
          {/* Close form — wired in Task 6 */}
        </div>
      )}
    </Modal>
  );
}
