"use client";

import { useEffect, useMemo, useState } from "react";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import Modal from "./Modal";
import { ModifyOrderQuoteTelemetry } from "./QuoteTelemetry";
import { fmtPrice } from "@/lib/positionUtils";
import { OrderLegPills, type OrderLeg as UnifiedOrderLeg } from "@/lib/order";
import { useClientAttemptId } from "@/components/ticker-detail/useClientAttemptId";
import { getReasonToast } from "@/lib/orderReasonCodes";
import {
  seedTicketFromPosition,
  applyQtyChip,
  type Intent,
  type TicketDraft,
} from "@/lib/positionOrderPresets";

type Props = {
  position: PortfolioPosition;
  prices: Record<string, PriceData>;
  onClose: () => void;
  onSubmitted?: (orderId: string) => void;
};

function errorFromResponseBody(
  body: Record<string, unknown> | null | undefined,
  fallback: string,
): string {
  if (body && typeof body === "object") {
    const code = (body as { reason_code?: unknown }).reason_code;
    if (typeof code === "string" && code.length > 0)
      return getReasonToast(code).copy;
    const err = (body as { error?: unknown }).error;
    if (typeof err === "string" && err.length > 0) return err;
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
  }
  return fallback;
}

function unifiedLegsFromPosition(pos: PortfolioPosition): UnifiedOrderLeg[] {
  return pos.legs
    .filter((l) => l.type !== "Stock" && l.strike != null)
    .map((leg, i) => ({
      id: `leg-${i}`,
      action: leg.direction === "LONG" ? "BUY" : "SELL",
      direction: leg.direction,
      strike: leg.strike!,
      type: leg.type === "Call" ? "Call" : "Put",
      expiry: pos.expiry.replace(/-/g, ""),
      quantity: Math.abs(leg.contracts),
    }));
}

export default function PositionOrderModal({
  position,
  prices,
  onClose,
  onSubmitted,
}: Props) {
  const [intent, setIntent] = useState<Intent>("close");

  const draft: TicketDraft = useMemo(
    () => seedTicketFromPosition(position, intent, prices),
    [position, intent, prices],
  );

  const fullQty = Math.abs(position.contracts);
  const isCombo = draft.payload.type === "combo";

  const [qtyText, setQtyText] = useState<string>(String(fullQty));
  const [priceText, setPriceText] = useState<string>(
    Number.isFinite(draft.payload.limitPrice)
      ? draft.payload.limitPrice.toFixed(2)
      : "",
  );
  const [outsideRth, setOutsideRth] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const attemptId = useClientAttemptId({ ticker: position.ticker });

  // Reseed price ONLY when intent toggles (Close ↔ Add). We deliberately do NOT
  // reseed on live mid changes — that would clobber whatever the user has typed
  // every time the WS tick lands. The live mid keeps ticking in the BID/MID/ASK
  // reference row at the bottom; the input is the user's to edit.
  useEffect(() => {
    if (Number.isFinite(draft.payload.limitPrice)) {
      setPriceText(draft.payload.limitPrice.toFixed(2));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intent]);

  const parsedQty =
    qtyText.trim() === ""
      ? NaN
      : position.structure_type === "Stock"
        ? parseFloat(qtyText)
        : parseInt(qtyText, 10);
  const parsedPrice =
    priceText.trim() === "" || priceText.trim() === "-"
      ? NaN
      : parseFloat(priceText);
  const isValidQty = Number.isFinite(parsedQty) && parsedQty > 0;
  const isValidPrice =
    Number.isFinite(parsedPrice) &&
    (isCombo ? parsedPrice !== 0 : parsedPrice > 0);

  const handleChip = (pct: number) => {
    const next = applyQtyChip(fullQty, pct);
    setQtyText(String(next));
    attemptId.onFieldEdit("quantity");
  };

  const handleSubmit = async () => {
    if (submitting || !isValidQty || !isValidPrice) return;
    setSubmitting(true);
    setError(null);
    try {
      const clampedQty =
        intent === "close"
          ? Math.max(1, Math.min(fullQty, parsedQty))
          : Math.max(1, parsedQty);
      attemptId.markSubmitted();
      const body = {
        ...draft.payload,
        quantity: clampedQty,
        limitPrice: parsedPrice,
        client_attempt_id: attemptId.id,
        ...(outsideRth && !isCombo ? { outsideRth: true } : {}),
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

  const submitLabel = submitting
    ? intent === "close"
      ? "Submitting close…"
      : "Submitting add…"
    : intent === "close"
      ? "Submit close"
      : "Submit add";

  const priceData: PriceData | null = useMemo(() => {
    if (draft.referenceBid == null || draft.referenceAsk == null) return null;
    return {
      symbol: position.ticker,
      last: draft.referenceMid ?? null,
      lastIsCalculated: true,
      bid: draft.referenceBid,
      ask: draft.referenceAsk,
      bidSize: null,
      askSize: null,
      volume: null,
      high: null,
      low: null,
      open: null,
      close: null,
      week52High: null,
      week52Low: null,
      avgVolume: null,
      delta: null,
      gamma: null,
      theta: null,
      vega: null,
      impliedVol: null,
      undPrice: null,
      timestamp: new Date().toISOString(),
    } as unknown as PriceData;
  }, [
    draft.referenceBid,
    draft.referenceMid,
    draft.referenceAsk,
    position.ticker,
  ]);

  const partial = intent === "close" && isValidQty && parsedQty < fullQty;

  const unifiedLegs = useMemo(
    () => unifiedLegsFromPosition(position),
    [position],
  );

  return (
    <Modal
      open={true}
      onClose={onClose}
      title={`Order — ${position.ticker} ${position.structure}`}
      className={
        isCombo
          ? "modify-order-modal modify-order-modal-combo"
          : "modify-order-modal"
      }
    >
      <div className={`modify-dialog${isCombo ? " modify-dialog-combo" : ""}`}>
        <div className="modify-order-info">
          <strong>{position.ticker}</strong>
          <span
            className={`pill ${position.direction === "LONG" ? "accum" : "distrib"}`}
          >
            {position.direction}
          </span>
          <span>{position.structure}</span>
          <span>{fullQty}x</span>
        </div>

        <div
          className="position-order-intent-bar"
          role="group"
          aria-label="Order intent"
        >
          <button
            type="button"
            className={`preset-tile ${intent === "close" ? "active" : ""}`}
            aria-pressed={intent === "close"}
            onClick={() => {
              setIntent("close");
              attemptId.onFieldEdit("intent");
            }}
          >
            Close
          </button>
          <button
            type="button"
            className={`preset-tile ${intent === "add" ? "active" : ""}`}
            aria-pressed={intent === "add"}
            onClick={() => {
              setIntent("add");
              attemptId.onFieldEdit("intent");
            }}
          >
            Add
          </button>
        </div>

        <div
          className={`modify-layout${isCombo ? " modify-layout-combo" : ""}`}
        >
          <div className="modify-primary-panel">
            <ModifyOrderQuoteTelemetry priceData={priceData} />

            <div className="modify-price-section">
              <div
                className={`modify-field-grid${isCombo ? " modify-field-grid-combo" : ""}`}
              >
                <label className="modify-field" htmlFor="position-order-qty">
                  <span className="modify-price-label">Quantity</span>
                  <div className="modify-price-input-row">
                    <input
                      id="position-order-qty"
                      className="modify-price-input"
                      type="text"
                      inputMode="decimal"
                      value={qtyText}
                      onChange={(e) => {
                        setQtyText(e.target.value);
                        attemptId.onFieldEdit("quantity");
                      }}
                    />
                  </div>
                </label>

                <label className="modify-field" htmlFor="position-order-price">
                  <span className="modify-price-label">
                    {isCombo ? "Net Limit Price" : "Limit Price"}
                  </span>
                  <div className="modify-price-input-row">
                    <span className="modify-price-prefix">$</span>
                    <input
                      id="position-order-price"
                      className="modify-price-input"
                      type="text"
                      inputMode="decimal"
                      value={priceText}
                      onChange={(e) => {
                        setPriceText(e.target.value);
                        attemptId.onFieldEdit("limitPrice");
                      }}
                      autoFocus
                    />
                  </div>
                </label>
              </div>

              {intent === "close" && (
                <div
                  className="position-order-chip-row"
                  role="group"
                  aria-label="Close size chips"
                >
                  <button
                    type="button"
                    className="btn-quick"
                    onClick={() => handleChip(1.0)}
                  >
                    100%
                  </button>
                  <button
                    type="button"
                    className="btn-quick"
                    onClick={() => handleChip(0.5)}
                  >
                    50%
                  </button>
                  <button
                    type="button"
                    className="btn-quick"
                    onClick={() => handleChip(0.25)}
                  >
                    25%
                  </button>
                </div>
              )}

              <div className="modify-quick-section">
                <span className="modify-price-label">Reference Price</span>
                <div className="modify-quick-buttons">
                  <button
                    className="btn-quick"
                    disabled={draft.referenceBid == null}
                    onClick={() => {
                      if (draft.referenceBid != null) {
                        setPriceText(draft.referenceBid.toFixed(2));
                        attemptId.onFieldEdit("limitPrice");
                      }
                    }}
                  >
                    BID
                    {draft.referenceBid != null
                      ? ` ${draft.referenceBid.toFixed(2)}`
                      : ""}
                  </button>
                  <button
                    className="btn-quick"
                    disabled={draft.referenceMid == null}
                    onClick={() => {
                      if (draft.referenceMid != null) {
                        setPriceText(draft.referenceMid.toFixed(2));
                        attemptId.onFieldEdit("limitPrice");
                      }
                    }}
                  >
                    MID
                    {draft.referenceMid != null
                      ? ` ${draft.referenceMid.toFixed(2)}`
                      : ""}
                  </button>
                  <button
                    className="btn-quick"
                    disabled={draft.referenceAsk == null}
                    onClick={() => {
                      if (draft.referenceAsk != null) {
                        setPriceText(draft.referenceAsk.toFixed(2));
                        attemptId.onFieldEdit("limitPrice");
                      }
                    }}
                  >
                    ASK
                    {draft.referenceAsk != null
                      ? ` ${draft.referenceAsk.toFixed(2)}`
                      : ""}
                  </button>
                </div>
              </div>

              {!isCombo && (
                <label className="modify-rth-toggle">
                  <input
                    type="checkbox"
                    checked={outsideRth}
                    onChange={(e) => setOutsideRth(e.target.checked)}
                  />
                  <span className="modify-rth-label">FILL OUTSIDE RTH</span>
                  <span className="modify-rth-hint">
                    Pre-market &amp; after hours
                  </span>
                </label>
              )}

              {partial && (
                <p className="partial-close-note">
                  Partial close — {parsedQty} of {fullQty} contracts
                </p>
              )}

              {isValidPrice &&
                draft.referenceMid != null &&
                Math.abs(parsedPrice - draft.referenceMid) >= 0.005 && (
                  <div
                    className={`modify-delta ${parsedPrice - draft.referenceMid > 0 ? "positive" : "negative"}`}
                  >
                    {parsedPrice - draft.referenceMid > 0 ? "+" : ""}
                    {fmtPrice(Math.abs(parsedPrice - draft.referenceMid))} from
                    mid {fmtPrice(draft.referenceMid)}
                  </div>
                )}

              {error && <p className="order-error">{error}</p>}
            </div>
          </div>

          {isCombo && unifiedLegs.length > 0 && (
            <div className="modify-secondary-panel">
              <div style={{ marginBottom: "12px" }}>
                <OrderLegPills legs={unifiedLegs} />
              </div>
              <div className="modify-section-heading">
                <span className="modify-price-label">Legs</span>
                <span className="modify-section-hint">
                  Read-only — leg editing comes in a follow-up
                </span>
              </div>
            </div>
          )}
        </div>

        <div className="modify-actions">
          <button
            className="btn-secondary"
            onClick={onClose}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            className="btn-primary"
            onClick={handleSubmit}
            disabled={submitting || !isValidQty || !isValidPrice}
            aria-label={submitLabel}
          >
            {submitLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}
