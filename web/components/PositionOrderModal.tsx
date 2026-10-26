"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { PortfolioPosition } from "@/lib/types";
import type { FutuAccountEnv } from "@/lib/futuAccountEnv";
import { futuScopeQuery, useFutuAccountEnv } from "@/lib/futuAccountEnv";
import {
  scopesMatch,
  ticketAnchorFromTable,
  type TicketAnchor,
} from "@/lib/orderActionScope";
import {
  futuCapabilitiesForEnv,
  futuTradingEnabledForEnv,
  useFutuTrading,
} from "@/lib/useFutuTrading";
import {
  FUTU_ORDER_TYPE_FIELDS,
  FUTU_TRAIL_TYPES,
  buildFutuOrderTerms,
  fmtFutuReference,
  futuReferenceText,
  parseStrictDecimal,
  futuOrderConfirmText,
  futuTicketChoices,
  type FutuSecType,
  type FutuTicketSelection,
  type FutuTrailType,
} from "@/lib/futuOrderTypes";
import {
  checkNakedShortRisk,
  type NakedShortPortfolio,
  type OrderPayload,
} from "@/lib/nakedShortGuard";

function toNakedShortPortfolio(
  positions: PortfolioPosition[],
): NakedShortPortfolio {
  return {
    positions: positions.map((p) => ({
      ticker: p.ticker,
      structure_type: p.structure_type,
      contracts: p.contracts,
      direction: p.direction,
      expiry: p.expiry,
      legs: p.legs.map((l) => ({
        direction: l.direction,
        type: l.type,
        contracts: l.contracts,
        strike: l.strike,
      })),
    })),
  };
}
import type { PriceData } from "@/lib/pricesProtocol";
import Modal from "./Modal";
import { ModifyOrderQuoteTelemetry } from "./QuoteTelemetry";
import { fmtPrice } from "@/lib/positionUtils";
import { OrderLegPills, type OrderLeg as UnifiedOrderLeg } from "@/lib/order";
import { OrderTifSelector } from "@/lib/order/components/OrderTifSelector";
import { OrderChoiceSelector } from "@/lib/order/components/OrderChoiceSelector";
import { useClientAttemptId } from "@/components/ticker-detail/useClientAttemptId";
import { submitPlaceOrder } from "@/lib/order/submitPlace";
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
  futuTicket?: {
    env: FutuAccountEnv;
    accountTail: string | null;
    portfolioPositions: PortfolioPosition[];
  };
  /** Broker/env captured when the ticket opened (F2). */
  ticketAnchor?: TicketAnchor | null;
};

function parseInputNumber(text: string): number {
  const t = text.trim();
  return t === "" || t === "-" ? NaN : parseFloat(t);
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
  futuTicket,
  ticketAnchor = null,
}: Props) {
  const futuEnv = useFutuAccountEnv();
  const futuTrading = useFutuTrading();
  const [intent, setIntent] = useState<Intent>("close");

  const draft: TicketDraft = useMemo(
    () => seedTicketFromPosition(position, intent, prices),
    [position, intent, prices],
  );

  const fullQty = Math.abs(position.contracts);
  const isCombo = draft.payload.type === "combo";
  const futuBlockedCombo = Boolean(futuTicket && isCombo);

  const [qtyText, setQtyText] = useState<string>(String(fullQty));
  const [priceText, setPriceText] = useState<string>(
    Number.isFinite(draft.payload.limitPrice)
      ? draft.payload.limitPrice.toFixed(2)
      : "",
  );
  const [outsideRth, setOutsideRth] = useState(false);
  const [tif, setTif] = useState<"DAY" | "GTC">(draft.payload.tif ?? "DAY");
  const [futuSel, setFutuSel] = useState<FutuTicketSelection>({});
  const [auxText, setAuxText] = useState("");
  const [trailType, setTrailType] = useState<FutuTrailType>("RATIO");
  const [trailValueText, setTrailValueText] = useState("");
  const [trailSpreadText, setTrailSpreadText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const attemptId = useClientAttemptId({ ticker: position.ticker });

  const currentAnchor = futuTicket
    ? ticketAnchorFromTable(true, futuTicket.env)
    : ticketAnchorFromTable(false, futuEnv);

  useEffect(() => {
    if (!ticketAnchor) return;
    if (!scopesMatch(ticketAnchor, currentAnchor)) onClose();
  }, [ticketAnchor, currentAnchor, onClose]);

  useEffect(() => {
    if (!futuTicket || !ticketAnchor) return;
    if (futuTrading === null) return;
    if (!futuTradingEnabledForEnv(futuTrading, futuTicket.env)) onClose();
  }, [futuTrading, futuTicket, ticketAnchor, onClose]);

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
  // Futu single-leg: the whole text must be a decimal (no prefix parsing).
  const futuSingleLeg = Boolean(futuTicket) && !isCombo;
  const parsedPrice = futuSingleLeg
    ? parseStrictDecimal(priceText)
    : parseInputNumber(priceText);
  const isValidQty = Number.isFinite(parsedQty) && parsedQty > 0;
  const isValidPrice =
    Number.isFinite(parsedPrice) &&
    (isCombo ? parsedPrice !== 0 : parsedPrice > 0);

  // Futu single-leg: type / TIF / session only from capabilities[env].tuples.
  const futuSec: FutuSecType | null =
    futuTicket && !isCombo
      ? draft.payload.type === "stock"
        ? "STK"
        : "OPT"
      : null;
  const futuChoices =
    futuTicket && futuSec
      ? futuTicketChoices(
          futuCapabilitiesForEnv(futuTrading, futuTicket.env),
          futuSec,
          futuSel,
        )
      : null;
  const futuType = futuChoices?.orderType ?? null;
  const futuTypeFields = futuType ? FUTU_ORDER_TYPE_FIELDS[futuType] : null;
  // A change of the effective order type clears the manual limit, trigger
  // and trail values, so a term typed for an earlier type never comes back.
  const prevFutuType = useRef(futuType);
  useEffect(() => {
    const prev = prevFutuType.current;
    prevFutuType.current = futuType;
    if (prev == null || futuType == null || prev === futuType) return;
    setPriceText("");
    setAuxText("");
    setTrailValueText("");
    setTrailSpreadText("");
  }, [futuType]);
  const showLimit = !futuChoices || futuTypeFields?.limit === true;
  const showAux = futuTypeFields?.aux === true;
  const futuTerms =
    futuChoices && futuSec && futuType
      ? buildFutuOrderTerms({
          orderType: futuType,
          sec: futuSec,
          session: futuChoices.session,
          limitPrice: parsedPrice,
          auxPrice: parseStrictDecimal(auxText),
          trailType,
          trailValue: parseStrictDecimal(trailValueText),
          trailSpread: parseStrictDecimal(trailSpreadText),
        })
      : null;
  const futuReady = Boolean(futuTerms?.ok && futuChoices?.tif);
  const isValidTerms = futuChoices ? futuReady : isValidPrice;
  const clampedQty =
    intent === "close"
      ? Math.max(1, Math.min(fullQty, parsedQty))
      : Math.max(1, parsedQty);
  const futuConfirm =
    futuTerms?.ok && futuChoices?.tif && isValidQty
      ? futuOrderConfirmText({
          action: draft.payload.action,
          quantity: clampedQty,
          symbol: position.ticker,
          tif: futuChoices.tif,
          fields: futuTerms.fields,
        })
      : null;

  const editFutu = (field: string, apply: () => void) => {
    apply();
    attemptId.onFieldEdit(field);
  };
  // BID / MID / ASK fill the limit, or the trigger for types without a limit.
  const referenceTarget: "limit" | "aux" | null = showLimit
    ? "limit"
    : showAux
      ? "aux"
      : null;
  // Futu keeps the full reference (a 0.5123 bid stays 0.5123; precision the
  // grid does not support is refused by the server, never rounded here).
  const referenceText = (value: number) =>
    futuSingleLeg ? futuReferenceText(value) : value.toFixed(2);
  const referenceCaption = (value: number) =>
    futuSingleLeg ? fmtFutuReference(value) : value.toFixed(2);
  const applyReference = (value: number) => {
    if (referenceTarget === "limit") {
      setPriceText(referenceText(value));
      attemptId.onFieldEdit("limitPrice");
    } else if (referenceTarget === "aux") {
      setAuxText(referenceText(value));
      attemptId.onFieldEdit("auxPrice");
    }
  };

  const handleChip = (pct: number) => {
    const next = applyQtyChip(fullQty, pct);
    setQtyText(String(next));
    attemptId.onFieldEdit("quantity");
  };

  const handleSubmit = async () => {
    if (
      submitting ||
      !isValidQty ||
      !isValidTerms ||
      futuBlockedCombo ||
      nakedShortWarning
    )
      return;
    if (ticketAnchor && !scopesMatch(ticketAnchor, currentAnchor)) {
      setError("Account scope changed — reopen the ticket.");
      return;
    }
    if (futuTicket && !futuTradingEnabledForEnv(futuTrading, futuTicket.env)) {
      setError("Futu trading is disabled for this environment.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        ...draft.payload,
        quantity: clampedQty,
        limitPrice: parsedPrice,
        tif,
        ...(outsideRth && !isCombo && !futuTicket ? { outsideRth: true } : {}),
      };
      if (futuChoices && futuTerms?.ok) {
        // Only the fields of the chosen type; absent fields are never sent.
        delete body.limitPrice;
        Object.assign(body, { tif: futuChoices.tif }, futuTerms.fields);
      }
      const placed = await submitPlaceOrder(
        attemptId,
        body,
        futuTicket ? { scopeQuery: futuScopeQuery(futuTicket.env) } : undefined,
      );
      if (placed.kind !== "ok") {
        setError(placed.message);
        return;
      }
      const orderId =
        typeof placed.json.orderId === "string" ? placed.json.orderId : "";
      onSubmitted?.(orderId);
      onClose();
    } catch {
      setError("Network error placing order");
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
  const nakedShortWarning = useMemo(() => {
    if (!futuTicket) return null;
    if (draft.payload.action !== "SELL") return null;
    const qty = !isNaN(parsedQty) && parsedQty > 0 ? parsedQty : 1;
    const payload = {
      ...draft.payload,
      quantity: qty,
      limitPrice: Number.isFinite(parsedPrice) ? parsedPrice : 1,
      tif,
    };
    const guardPortfolio = toNakedShortPortfolio(futuTicket.portfolioPositions);
    const result = checkNakedShortRisk(payload as OrderPayload, guardPortfolio);
    return result.allowed ? null : (result.reason ?? null);
  }, [futuTicket, draft.payload, parsedQty, parsedPrice, tif]);

  const unifiedLegs = useMemo(
    () => unifiedLegsFromPosition(position),
    [position],
  );

  return (
    <Modal
      open={true}
      onClose={onClose}
      title={`Order — ${position.ticker} ${position.structure}${
        futuTicket
          ? ` · ${futuTicket.env === "paper" ? "SIMULATE" : "REAL"}${
              futuTicket.accountTail ? ` · …${futuTicket.accountTail}` : ""
            }`
          : ""
      }`}
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
              {futuChoices && (
                <div className="modify-quick-section">
                  <span className="modify-price-label">Order Type</span>
                  <OrderChoiceSelector
                    ariaLabel="Order type"
                    options={futuChoices.orderTypes}
                    value={futuChoices.orderType}
                    className="modify-quick-buttons"
                    onChange={(orderType) =>
                      editFutu("orderType", () =>
                        setFutuSel((s) => ({ ...s, orderType })),
                      )
                    }
                  />
                </div>
              )}
              {futuChoices &&
                futuTrading !== null &&
                futuChoices.orderTypes.length === 0 && (
                  <p className="order-error">
                    No Futu order types are enabled for this environment.
                  </p>
                )}

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

                {showAux && (
                  <label className="modify-field" htmlFor="position-order-aux">
                    <span className="modify-price-label">Trigger Price</span>
                    <div className="modify-price-input-row">
                      <span className="modify-price-prefix">$</span>
                      <input
                        id="position-order-aux"
                        className="modify-price-input"
                        type="text"
                        inputMode="decimal"
                        value={auxText}
                        onChange={(e) =>
                          editFutu("auxPrice", () => setAuxText(e.target.value))
                        }
                      />
                    </div>
                  </label>
                )}

                {showLimit && (
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
                )}

                {futuTypeFields?.trail && (
                  <>
                    <div className="modify-quick-section">
                      <span className="modify-price-label">Trail Type</span>
                      <OrderChoiceSelector
                        ariaLabel="Trail type"
                        options={FUTU_TRAIL_TYPES}
                        value={trailType}
                        className="modify-quick-buttons"
                        onChange={(value) =>
                          editFutu("trailType", () => setTrailType(value))
                        }
                      />
                    </div>
                    <label
                      className="modify-field"
                      htmlFor="position-order-trail-value"
                    >
                      <span className="modify-price-label">Trail Value</span>
                      <div className="modify-price-input-row">
                        <span className="modify-price-prefix">
                          {trailType === "RATIO" ? "%" : "$"}
                        </span>
                        <input
                          id="position-order-trail-value"
                          className="modify-price-input"
                          type="text"
                          inputMode="decimal"
                          value={trailValueText}
                          onChange={(e) =>
                            editFutu("trailValue", () =>
                              setTrailValueText(e.target.value),
                            )
                          }
                        />
                      </div>
                    </label>
                  </>
                )}

                {futuTypeFields?.spread && (
                  <label
                    className="modify-field"
                    htmlFor="position-order-trail-spread"
                  >
                    <span className="modify-price-label">Trail Spread</span>
                    <div className="modify-price-input-row">
                      <span className="modify-price-prefix">$</span>
                      <input
                        id="position-order-trail-spread"
                        className="modify-price-input"
                        type="text"
                        inputMode="decimal"
                        value={trailSpreadText}
                        onChange={(e) =>
                          editFutu("trailSpread", () =>
                            setTrailSpreadText(e.target.value),
                          )
                        }
                      />
                    </div>
                  </label>
                )}
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
                    disabled={draft.referenceBid == null || !referenceTarget}
                    onClick={() => {
                      if (draft.referenceBid != null)
                        applyReference(draft.referenceBid);
                    }}
                  >
                    BID
                    {draft.referenceBid != null
                      ? ` ${referenceCaption(draft.referenceBid)}`
                      : ""}
                  </button>
                  <button
                    className="btn-quick"
                    disabled={draft.referenceMid == null || !referenceTarget}
                    onClick={() => {
                      if (draft.referenceMid != null)
                        applyReference(draft.referenceMid);
                    }}
                  >
                    MID
                    {draft.referenceMid != null
                      ? ` ${referenceCaption(draft.referenceMid)}`
                      : ""}
                  </button>
                  <button
                    className="btn-quick"
                    disabled={draft.referenceAsk == null || !referenceTarget}
                    onClick={() => {
                      if (draft.referenceAsk != null)
                        applyReference(draft.referenceAsk);
                    }}
                  >
                    ASK
                    {draft.referenceAsk != null
                      ? ` ${referenceCaption(draft.referenceAsk)}`
                      : ""}
                  </button>
                </div>
              </div>

              {futuChoices && (
                <>
                  <div className="modify-quick-section">
                    <span className="modify-price-label">Time in Force</span>
                    <OrderChoiceSelector
                      ariaLabel="Time in force"
                      options={futuChoices.tifs}
                      value={futuChoices.tif}
                      className="modify-quick-buttons"
                      onChange={(value) =>
                        editFutu("tif", () =>
                          setFutuSel((s) => ({ ...s, tif: value })),
                        )
                      }
                    />
                  </div>
                  {futuSec === "STK" && (
                    <div className="modify-quick-section">
                      <span className="modify-price-label">Session</span>
                      <OrderChoiceSelector
                        ariaLabel="Session"
                        options={futuChoices.sessions}
                        value={futuChoices.session}
                        className="modify-quick-buttons"
                        onChange={(value) =>
                          editFutu("session", () =>
                            setFutuSel((s) => ({ ...s, session: value })),
                          )
                        }
                      />
                    </div>
                  )}
                </>
              )}

              {!isCombo && !futuChoices && (
                <>
                  <div className="modify-quick-section">
                    <span className="modify-price-label">Time in Force</span>
                    <OrderTifSelector
                      tif={tif}
                      className="modify-quick-buttons"
                      onChange={(value) => {
                        setTif(value);
                        attemptId.onFieldEdit("tif");
                      }}
                    />
                  </div>

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
                </>
              )}

              {isCombo && (
                <div className="modify-quick-section">
                  <span className="modify-price-label">Time in Force</span>
                  <OrderTifSelector
                    tif={tif}
                    className="modify-quick-buttons"
                    onChange={(value) => {
                      setTif(value);
                      attemptId.onFieldEdit("tif");
                    }}
                  />
                </div>
              )}

              {partial && (
                <p className="partial-close-note">
                  Partial close — {parsedQty} of {fullQty} contracts
                </p>
              )}

              {showLimit &&
                isValidPrice &&
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

              {futuConfirm && (
                <p className="futu-order-confirm" data-testid="futu-order-confirm">
                  {futuConfirm}
                </p>
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
            disabled={
              submitting || !isValidQty || !isValidTerms || attemptId.uncertain
            }
            aria-label={submitLabel}
          >
            {submitLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}
