"use client";

import { useState } from "react";
import {
  isRegimeOverrideReasonValid,
  type RegimeBlockResponse,
  type RegimeResizeResponse,
} from "@/lib/order/regimeGate";

type Props =
  | {
      kind: "block";
      payload: RegimeBlockResponse;
      onConfirm: (overrideReason: string) => void;
      onCancel: () => void;
      onResize?: never;
      suggestedQuantity?: never;
      currentQuantity?: never;
    }
  | {
      kind: "resize";
      payload: RegimeResizeResponse;
      onConfirm?: never;
      onCancel: () => void;
      onResize: (newQuantity: number) => void;
      suggestedQuantity: number | null;
      currentQuantity: number;
    };

export function RegimeBlockModal(props: Props) {
  const [reason, setReason] = useState("");
  const [resizeQty, setResizeQty] = useState<string>(
    props.kind === "resize" && props.suggestedQuantity != null
      ? String(props.suggestedQuantity)
      : "",
  );

  const minChars =
    props.kind === "block"
      ? (props.payload.override_min_reason_chars ?? 10)
      : 0;
  const reasonOk = isRegimeOverrideReasonValid(reason, minChars);
  const resizeOk =
    props.kind === "resize" &&
    Number.isFinite(Number(resizeQty)) &&
    Number(resizeQty) > 0 &&
    Number(resizeQty) < props.currentQuantity;

  const titleText =
    props.kind === "block"
      ? "Order blocked by regime gate"
      : "Order exceeds regime cap";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={titleText}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
    >
      <div
        style={{
          background: "var(--color-bg, #0a0a0a)",
          color: "var(--color-text, #f5f5f5)",
          border: "1px solid var(--color-border, #333)",
          borderRadius: 4,
          padding: 24,
          maxWidth: 520,
          width: "90%",
          fontFamily: "var(--font-sans, sans-serif)",
        }}
      >
        <h2 style={{ marginTop: 0, fontSize: 16 }}>{titleText}</h2>
        <div
          style={{
            fontSize: 12,
            opacity: 0.8,
            marginBottom: 12,
            fontFamily: "var(--font-mono, monospace)",
          }}
        >
          {props.kind === "block" ? (
            <>
              binding={props.payload.binding_tier} ({props.payload.binding_side}
              ){" · "}vcg={props.payload.vcg_tier} · cri=
              {props.payload.cri_tier}
            </>
          ) : (
            <>
              tier={props.payload.binding_tier} · max_loss=$
              {props.payload.max_loss_usd?.toFixed(0) ?? "∞"} · cap=$
              {props.payload.max_loss_cap_usd.toFixed(0)} · cover=
              {props.payload.cover_ratio}
            </>
          )}
        </div>
        <p style={{ fontSize: 14, marginBottom: 16 }}>{props.payload.detail}</p>

        {props.kind === "block" ? (
          <>
            <label style={{ display: "block", fontSize: 12, marginBottom: 6 }}>
              Override reason (≥{minChars} chars, audited):
            </label>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={3}
              autoFocus
              style={{
                width: "100%",
                fontFamily: "var(--font-mono, monospace)",
                fontSize: 12,
                background: "var(--color-bg-elevated, #1a1a1a)",
                color: "inherit",
                border: "1px solid var(--color-border, #333)",
                borderRadius: 4,
                padding: 8,
                resize: "vertical",
              }}
            />
            <div style={{ fontSize: 11, opacity: 0.6, marginTop: 4 }}>
              {reason.trim().length} / {minChars}
            </div>
          </>
        ) : (
          <>
            <label style={{ display: "block", fontSize: 12, marginBottom: 6 }}>
              Trim contract count (current: {props.currentQuantity}
              {props.suggestedQuantity != null
                ? `, suggested: ${props.suggestedQuantity}`
                : ""}
              ):
            </label>
            <input
              type="number"
              min={1}
              max={props.currentQuantity - 1}
              value={resizeQty}
              onChange={(e) => setResizeQty(e.target.value)}
              autoFocus
              style={{
                width: "100%",
                fontFamily: "var(--font-mono, monospace)",
                fontSize: 12,
                background: "var(--color-bg-elevated, #1a1a1a)",
                color: "inherit",
                border: "1px solid var(--color-border, #333)",
                borderRadius: 4,
                padding: 8,
              }}
            />
          </>
        )}

        <div
          style={{
            display: "flex",
            gap: 8,
            marginTop: 20,
            justifyContent: "flex-end",
          }}
        >
          <button
            type="button"
            onClick={props.onCancel}
            style={{
              padding: "6px 16px",
              fontSize: 12,
              background: "transparent",
              color: "inherit",
              border: "1px solid var(--color-border, #333)",
              borderRadius: 4,
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
          {props.kind === "block" ? (
            <button
              type="button"
              disabled={!reasonOk}
              onClick={() => props.onConfirm(reason)}
              style={{
                padding: "6px 16px",
                fontSize: 12,
                background: reasonOk
                  ? "var(--color-warn, #f59e0b)"
                  : "var(--color-bg-disabled, #333)",
                color: "var(--color-bg, #0a0a0a)",
                border: "none",
                borderRadius: 4,
                cursor: reasonOk ? "pointer" : "not-allowed",
                opacity: reasonOk ? 1 : 0.5,
              }}
            >
              Override and submit
            </button>
          ) : (
            <button
              type="button"
              disabled={!resizeOk}
              onClick={() => props.onResize(Number(resizeQty))}
              style={{
                padding: "6px 16px",
                fontSize: 12,
                background: resizeOk
                  ? "var(--color-accent, #22c55e)"
                  : "var(--color-bg-disabled, #333)",
                color: "var(--color-bg, #0a0a0a)",
                border: "none",
                borderRadius: 4,
                cursor: resizeOk ? "pointer" : "not-allowed",
                opacity: resizeOk ? 1 : 0.5,
              }}
            >
              Apply resize
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
