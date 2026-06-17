"use client";

import { useCallback, useMemo, useState, type ReactNode } from "react";
import Link from "next/link";
import type { OpenOrder, PortfolioData, PortfolioPosition } from "@/lib/types";
import type { PriceData, DepthBook, Trade } from "@/lib/pricesProtocol";
import { parseOptionKey } from "@/lib/pricesProtocol";
import type { OrderPrefill } from "@/lib/TickerDetailContext";
import { OrderBook } from "./OrderBook";
import { fmtPrice } from "@/lib/positionUtils";
import OrderErrorBanner from "@/components/OrderErrorBanner";
import { OrderConfirmSummary, type OrderSummary } from "@/lib/order";
import { OrderTifSelector } from "@/lib/order/components/OrderTifSelector";
import { useClientAttemptId } from "./useClientAttemptId";
import { getReasonToast } from "@/lib/orderReasonCodes";

/* ─── Types ─── */

type BookTabProps = {
  ticker: string;
  position: PortfolioPosition | null;
  prices: Record<string, PriceData>;
  openOrders: OpenOrder[];
  tickerPriceData: PriceData | null;
  /** Cockpit mode: render ONLY the L1 book region (wrapped in `.book-tab-only`),
   *  suppressing the standalone position summary / stock order form / open-orders
   *  list — in the cockpit those live in the always-docked Act column, so
   *  embedding them here would duplicate them. Legacy/mobile layout leaves this
   *  false (the full book tab). */
  bookOnly?: boolean;
  /** L2 depth montage + tape (Phase 3c). `depths`/`tape` are keyed by the depth
   *  key (`bookKey`): the bare symbol for stocks, the composite optionKey for
   *  options. `onPriceClick` publishes a click-to-fill prefill from the book/tape.
   *  In `bookOnly` mode the L2 OrderBook renders the entitled book for `bookKey`,
   *  falling back to the L1 panel when there is no entitled depth. */
  onPriceClick?: (p: Omit<OrderPrefill, "nonce">) => void;
  depths?: Record<string, DepthBook>;
  tape?: Record<string, Trade[]>;
  bookKey?: string;
  bookKind?: "stock" | "option" | "future";
  /** Threaded so a SELL against held shares can short-circuit to a close-out
   *  branch (Phase 3 consumer). Unused in the Phase-2 L1 shell. */
  portfolio?: PortfolioData | null;
};

type OrderAction = "BUY" | "SELL";

function errorFromResponseBody(
  body: Record<string, unknown> | null | undefined,
  fallback: string,
): string {
  if (body && typeof body === "object") {
    const code = body.reason_code;
    if (typeof code === "string" && code.length > 0) {
      return getReasonToast(code).copy;
    }
    const error = body.error;
    if (typeof error === "string" && error.length > 0) return error;
    const detail = body.detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
  }
  return fallback;
}

/** Window-head label. An option subject shows its $strike·right·expiry rather
 *  than just the underlying ("QQQ $692P 07/17/26"), with the underlying symbol
 *  linked to its stock page (`/QQQ`); stocks (and any unparseable key) fall back
 *  to the bare ticker text. The depth key is the canonical optionKey
 *  (SYMBOL_YYYYMMDD_STRIKE_RIGHT), so it carries everything the head needs. */
function bookHeadLabel(
  ticker: string,
  bookKind: "stock" | "option" | "future" | undefined,
  bookKey: string | undefined,
): ReactNode {
  if (bookKind === "option" && bookKey) {
    const oc = parseOptionKey(bookKey);
    if (oc) {
      const e = oc.expiry;
      const exp = /^\d{8}$/.test(e)
        ? `${e.slice(4, 6)}/${e.slice(6, 8)}/${e.slice(2, 4)}`
        : e;
      return (
        <>
          <Link href={`/${oc.symbol.toUpperCase()}`} className="book-sym-link">
            {oc.symbol}
          </Link>
          {` $${oc.strike}${oc.right} ${exp}`}
        </>
      );
    }
  }
  return ticker;
}

/* ─── L1 Order Book ─── */

function L1OrderBook({
  bid,
  ask,
  spread,
  last,
  lastLabel = "LAST",
  bidSize,
  askSize,
}: {
  bid: number | null;
  ask: number | null;
  spread: number | null;
  last: number | null;
  lastLabel?: string;
  bidSize: number | null;
  askSize: number | null;
}) {
  return (
    <div className="book-l1">
      <div
        className="book-section-header"
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: "11px",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          color: "var(--text-secondary)",
          marginBottom: "8px",
        }}
      >
        ORDER BOOK
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr auto 1fr",
          gap: "16px",
          alignItems: "center",
        }}
      >
        {/* Bid side */}
        <div style={{ textAlign: "center" }}>
          <div
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "10px",
              textTransform: "uppercase",
              color: "var(--text-secondary)",
              marginBottom: "4px",
            }}
          >
            BID
          </div>
          <div
            className="positive"
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "16px",
              fontWeight: 600,
            }}
          >
            {bid != null ? fmtPrice(bid) : "---"}
          </div>
          <div
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "11px",
              color: "var(--text-secondary)",
              marginTop: "2px",
            }}
          >
            {bidSize != null ? bidSize : "---"}
          </div>
        </div>

        {/* Spread */}
        <div style={{ textAlign: "center" }}>
          <div
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "10px",
              textTransform: "uppercase",
              color: "var(--text-secondary)",
              marginBottom: "4px",
            }}
          >
            SPREAD
          </div>
          <div
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "14px",
              color: "var(--text-primary, #e2e8f0)",
            }}
          >
            {spread != null ? spread.toFixed(2) : "---"}
          </div>
          <div
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "10px",
              color: "var(--text-secondary)",
              marginTop: "2px",
            }}
          >
            {last != null ? `${lastLabel} ${fmtPrice(last)}` : ""}
          </div>
        </div>

        {/* Ask side */}
        <div style={{ textAlign: "center" }}>
          <div
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "10px",
              textTransform: "uppercase",
              color: "var(--text-secondary)",
              marginBottom: "4px",
            }}
          >
            ASK
          </div>
          <div
            className="negative"
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "16px",
              fontWeight: 600,
            }}
          >
            {ask != null ? fmtPrice(ask) : "---"}
          </div>
          <div
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "11px",
              color: "var(--text-secondary)",
              marginTop: "2px",
            }}
          >
            {askSize != null ? askSize : "---"}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ─── Position Summary ─── */

function PositionSummary({ position }: { position: PortfolioPosition }) {
  return (
    <div style={{ marginTop: "16px" }}>
      <div
        className="book-section-header"
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: "11px",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          color: "var(--text-secondary)",
          marginBottom: "8px",
        }}
      >
        POSITION
      </div>
      <div className="instrument-summary-grid">
        <div className="pos-stat">
          <span className="pos-stat-label">DIRECTION</span>
          <span className="pos-stat-value">
            {position.direction} {position.contracts}x
          </span>
        </div>
        <div className="pos-stat">
          <span className="pos-stat-label">STRUCTURE</span>
          <span className="pos-stat-value">{position.structure}</span>
        </div>
        <div className="pos-stat">
          <span className="pos-stat-label">AVG COST</span>
          <span className="pos-stat-value">
            {position.entry_cost != null
              ? fmtPrice(
                  Math.abs(position.entry_cost) /
                    (position.contracts *
                      (position.structure_type === "Stock" ? 1 : 100)),
                )
              : "---"}
          </span>
        </div>
        <div className="pos-stat">
          <span className="pos-stat-label">MKT VALUE</span>
          <span className="pos-stat-value">
            {position.market_value != null
              ? fmtPrice(Math.abs(position.market_value))
              : "---"}
          </span>
        </div>
      </div>
    </div>
  );
}

/* ─── Open Orders List ─── */

function OpenOrdersList({ orders }: { orders: OpenOrder[] }) {
  return (
    <div style={{ marginTop: "16px" }}>
      <div
        className="book-section-header"
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: "11px",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          color: "var(--text-secondary)",
          marginBottom: "8px",
        }}
      >
        OPEN ORDERS ({orders.length})
      </div>
      {orders.map((o, i) => {
        const c = o.contract;
        const desc =
          c.secType === "OPT"
            ? `${c.symbol} ${c.expiry ?? ""} $${c.strike ?? ""} ${c.right ?? ""}`
            : c.symbol;

        return (
          <div
            key={o.permId || o.orderId || i}
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              padding: "6px 0",
              borderBottom: "1px solid var(--line-grid, #1e293b)",
              fontFamily: "var(--font-mono)",
              fontSize: "12px",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <span
                className={`pill ${o.action === "BUY" ? "accum" : "distrib"}`}
                style={{ fontSize: "9px" }}
              >
                {o.action}
              </span>
              <span>{desc}</span>
              <span style={{ color: "var(--text-secondary)" }}>
                {o.totalQuantity}x
              </span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
              <span>
                {o.limitPrice != null ? fmtPrice(o.limitPrice) : "MKT"}
              </span>
              <span
                style={{ color: "var(--text-secondary)", fontSize: "10px" }}
              >
                {o.tif} / {o.status}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ─── Stock Order Form ─── */

function StockOrderForm({
  ticker,
  position,
  bid,
  ask,
  mid,
}: {
  ticker: string;
  position: PortfolioPosition | null;
  bid: number | null;
  ask: number | null;
  mid: number | null;
}) {
  const defaultAction: OrderAction = position != null ? "SELL" : "BUY";
  const [action, setAction] = useState<OrderAction>(defaultAction);
  const [quantity, setQuantity] = useState(() => {
    if (position && position.structure_type === "Stock")
      return String(position.contracts);
    return "";
  });
  const [limitPrice, setLimitPrice] = useState("");
  const [tif, setTif] = useState<"DAY" | "GTC">("DAY");
  const [confirmStep, setConfirmStep] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const attemptId = useClientAttemptId({ ticker });

  const parsedQty = parseInt(quantity, 10);
  const parsedPrice = parseFloat(limitPrice);
  const isValid =
    !isNaN(parsedQty) &&
    parsedQty > 0 &&
    !isNaN(parsedPrice) &&
    parsedPrice > 0;

  // Calculate order summary for confirmation (stock: no multiplier)
  const orderSummary: OrderSummary | null = useMemo(() => {
    if (!isValid) return null;
    const totalCost = parsedQty * parsedPrice;
    const description = `${action} ${parsedQty} ${ticker} @ ${fmtPrice(parsedPrice)}`;
    return {
      description,
      totalCost: action === "SELL" ? -totalCost : totalCost,
    };
  }, [isValid, parsedQty, parsedPrice, action, ticker]);

  const handlePlace = useCallback(async () => {
    if (!confirmStep) {
      setConfirmStep(true);
      return;
    }

    setLoading(true);
    setError(null);
    setSuccess(null);

    try {
      attemptId.markSubmitted();
      const res = await fetch("/api/orders/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          type: "stock",
          symbol: ticker,
          action,
          quantity: parsedQty,
          limitPrice: parsedPrice,
          tif,
          client_attempt_id: attemptId.id,
        }),
      });
      const json = await res.json();
      if (!res.ok) {
        setError(errorFromResponseBody(json, "Order placement failed"));
        attemptId.markTerminal();
      } else {
        setSuccess(
          `Order placed: ${action} ${parsedQty} ${ticker} @ ${fmtPrice(parsedPrice)}`,
        );
        setConfirmStep(false);
        attemptId.markTerminal();
      }
    } catch {
      setError("Network error placing order");
      attemptId.markTerminal();
    } finally {
      setLoading(false);
    }
  }, [confirmStep, ticker, action, parsedQty, parsedPrice, tif, attemptId]);

  return (
    <div className="order-form" style={{ marginTop: "16px" }}>
      <div
        className="book-section-header"
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: "11px",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          color: "var(--text-secondary)",
          marginBottom: "8px",
        }}
      >
        STOCK ORDER
      </div>

      <div className="order-field">
        <label className="order-label">Action</label>
        <div className="order-action-buttons">
          <button
            className={`order-action-btn ${action === "BUY" ? "order-action-active order-action-buy" : ""}`}
            onClick={() => {
              setAction("BUY");
              setConfirmStep(false);
            }}
          >
            BUY
          </button>
          <button
            className={`order-action-btn ${action === "SELL" ? "order-action-active order-action-sell" : ""}`}
            onClick={() => {
              setAction("SELL");
              setConfirmStep(false);
            }}
          >
            SELL
          </button>
        </div>
      </div>

      <div className="order-field">
        <label className="order-label">Quantity</label>
        <input
          className="order-input"
          type="number"
          min="1"
          step="1"
          value={quantity}
          onChange={(e) => {
            setQuantity(e.target.value);
            setConfirmStep(false);
          }}
          placeholder="Shares"
        />
      </div>

      <div className="order-field">
        <label className="order-label">Limit Price</label>
        <div className="modify-price-input-row">
          <span className="modify-price-prefix">$</span>
          <input
            className="modify-price-input"
            type="number"
            step="0.01"
            min="0.01"
            value={limitPrice}
            onChange={(e) => {
              setLimitPrice(e.target.value);
              setConfirmStep(false);
            }}
            placeholder="0.00"
          />
        </div>
        <div className="modify-quick-buttons">
          <button
            className="btn-quick"
            disabled={bid == null}
            onClick={() => {
              if (bid != null) {
                setLimitPrice(bid.toFixed(2));
                setConfirmStep(false);
              }
            }}
          >
            BID
          </button>
          <button
            className="btn-quick"
            disabled={mid == null}
            onClick={() => {
              if (mid != null) {
                setLimitPrice(mid.toFixed(2));
                setConfirmStep(false);
              }
            }}
          >
            MID
          </button>
          <button
            className="btn-quick"
            disabled={ask == null}
            onClick={() => {
              if (ask != null) {
                setLimitPrice(ask.toFixed(2));
                setConfirmStep(false);
              }
            }}
          >
            ASK
          </button>
        </div>
      </div>

      <div className="order-field">
        <label className="order-label">Time in Force</label>
        <OrderTifSelector
          tif={tif}
          onChange={(value) => {
            setTif(value);
            setConfirmStep(false);
          }}
        />
      </div>

      <OrderErrorBanner error={error} />
      {success && <div className="order-success">{success}</div>}

      {/* Order Summary (shown in confirm step) */}
      {confirmStep && orderSummary && (
        <OrderConfirmSummary summary={orderSummary} variant="info" />
      )}

      <div className="order-submit">
        {confirmStep ? (
          <div className="order-confirm-row">
            <button
              className="btn-secondary"
              onClick={() => setConfirmStep(false)}
              disabled={loading}
            >
              Back
            </button>
            <button
              className={`btn-primary ${action === "SELL" ? "btn-danger" : ""}`}
              onClick={handlePlace}
              disabled={!isValid || loading}
            >
              {loading ? "Placing..." : "Confirm Order"}
            </button>
          </div>
        ) : (
          <button
            className="btn-primary"
            onClick={handlePlace}
            disabled={!isValid || loading}
            style={{ width: "100%" }}
          >
            Place Order
          </button>
        )}
      </div>
    </div>
  );
}

/* ─── Main BookTab ─── */

export default function BookTab({
  ticker,
  position,
  prices,
  openOrders,
  tickerPriceData,
  bookOnly = false,
  onPriceClick,
  depths,
  tape,
  bookKey,
  bookKind,
}: BookTabProps) {
  const priceData = tickerPriceData ?? prices[ticker] ?? null;
  const bid = priceData?.bid ?? null;
  const ask = priceData?.ask ?? null;
  const mid = bid != null && ask != null ? (bid + ask) / 2 : null;
  const spread = bid != null && ask != null ? ask - bid : null;
  const last = priceData?.last ?? null;
  const lastLabel = priceData?.lastIsCalculated ? "MARK" : "LAST";

  const l1 = (
    <L1OrderBook
      bid={bid}
      ask={ask}
      spread={spread}
      last={last}
      lastLabel={lastLabel}
      bidSize={priceData?.bidSize ?? null}
      askSize={priceData?.askSize ?? null}
    />
  );

  // Cockpit book-region: the L2 OrderBook (montage + tape) for the focused
  // `bookKey`, wrapped in `.book-tab-only`. The OrderBook renders the L1 panel
  // (`l1`) internally whenever there is no entitled depth book — so an account
  // without L2 entitlement still gets the existing top-of-book view. The
  // position summary / stock order form / open-orders list are owned by the
  // cockpit's Act column, so they are suppressed here to avoid duplication.
  if (bookOnly) {
    const book = (bookKey && depths?.[bookKey]) || null;
    const subjectTrades = (bookKey && tape?.[bookKey]) || [];
    return (
      <div className="book-tab book-tab-only" style={{ padding: "16px 0" }}>
        <OrderBook
          symbolLabel={bookHeadLabel(ticker, bookKind, bookKey)}
          kind={bookKind ?? "stock"}
          depth={book}
          trades={subjectTrades}
          last={last}
          lastLabel={lastLabel}
          bid={bid}
          ask={ask}
          l1Fallback={l1}
          onPriceClick={onPriceClick}
        />
      </div>
    );
  }

  return (
    <div className="book-tab" style={{ padding: "16px 0" }}>
      {l1}

      {position && <PositionSummary position={position} />}

      <StockOrderForm
        ticker={ticker}
        position={position}
        bid={bid}
        ask={ask}
        mid={mid}
      />

      {openOrders.length > 0 && <OpenOrdersList orders={openOrders} />}
    </div>
  );
}
