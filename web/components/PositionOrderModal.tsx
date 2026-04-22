"use client";

import { useState, useMemo } from "react";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import Modal from "./Modal";
import { buildCloseTicket, applyQtyChip } from "@/lib/positionOrderPresets";
import { useClientAttemptId } from "@/components/ticker-detail/useClientAttemptId";
import { getReasonToast } from "@/lib/orderReasonCodes";

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
          <ClosePresetForm
            position={position}
            prices={prices}
            onClose={onClose}
            onSubmitted={onSubmitted}
          />
        </div>
      )}
    </Modal>
  );
}

function errorFromResponseBody(
  body: Record<string, unknown> | null | undefined,
  fallback: string,
): string {
  if (body && typeof body === "object") {
    const code = (body as { reason_code?: unknown }).reason_code;
    if (typeof code === "string" && code.length > 0) {
      return getReasonToast(code).copy;
    }
    const err = (body as { error?: unknown }).error;
    if (typeof err === "string" && err.length > 0) return err;
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
  }
  return fallback;
}

function ClosePresetForm({
  position,
  prices,
  onClose,
  onSubmitted,
}: {
  position: PortfolioPosition;
  prices: Record<string, PriceData>;
  onClose: () => void;
  onSubmitted?: (orderId: string) => void;
}) {
  const draft = useMemo(
    () => buildCloseTicket(position, prices),
    [position, prices],
  );
  const fullQty = Math.abs(position.contracts);
  const [qty, setQty] = useState<number>(fullQty);
  const [limitPrice, setLimitPrice] = useState<number>(
    draft.payload.limitPrice,
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const attemptId = useClientAttemptId({ ticker: position.ticker });

  const handleChip = (pct: number) => setQty(applyQtyChip(fullQty, pct));

  const handleSubmit = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      // Clamp qty into [1, fullQty] so a manual over-type cannot flip a close
      // into an opening trade that slips past the server naked-short guard.
      const clampedQty = Math.max(1, Math.min(fullQty, qty));
      attemptId.markSubmitted();
      const body = {
        ...draft.payload,
        quantity: clampedQty,
        limitPrice,
        client_attempt_id: attemptId.id,
      };
      const res = await fetch("/api/orders/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(errorFromResponseBody(json, "Order placement failed"));
        attemptId.markTerminal();
        return;
      }
      const orderId = typeof json.orderId === "string" ? json.orderId : "";
      attemptId.markTerminal();
      onSubmitted?.(orderId);
      onClose();
    } catch {
      setError("Network error placing order");
      attemptId.markTerminal();
    } finally {
      setSubmitting(false);
    }
  };

  const partial = qty < fullQty;

  return (
    <div className="position-order-close-form">
      <div className="chip-row" role="group" aria-label="Close size chips">
        <button type="button" onClick={() => handleChip(1.0)}>
          100%
        </button>
        <button type="button" onClick={() => handleChip(0.5)}>
          50%
        </button>
        <button type="button" onClick={() => handleChip(0.25)}>
          25%
        </button>
      </div>

      <label>
        Quantity
        <input
          type="number"
          min={1}
          max={fullQty}
          value={qty}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") return;
            const parsed = parseInt(raw, 10);
            if (!Number.isFinite(parsed)) return;
            setQty(Math.max(1, Math.min(fullQty, parsed)));
          }}
        />
      </label>

      {partial && (
        <p className="partial-close-note">
          Partial close — {qty} of {fullQty} contracts
        </p>
      )}

      <label>
        Limit Price
        <input
          type="number"
          step="0.01"
          value={limitPrice}
          onChange={(e) => setLimitPrice(parseFloat(e.target.value) || 0)}
        />
      </label>

      {error && <p className="order-error">{error}</p>}

      <button
        type="button"
        onClick={handleSubmit}
        disabled={submitting || qty <= 0 || limitPrice <= 0}
        aria-label="Submit close"
      >
        {submitting ? "Submitting…" : "Submit close"}
      </button>
    </div>
  );
}
