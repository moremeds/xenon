"use client";

import Link from "next/link";
import type {
  OrdersData,
  OpenOrder,
  ExecutedOrder,
  OrderContract,
} from "@/lib/types";
import { fmtSignedPrice } from "@/lib/format";

type Props = {
  orders: OrdersData | null;
};

function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

// IB fill side comes through the orders API as BOT/SLD (orders.py:151).
// Normalize to BUY/SELL for a human-readable snapshot; pass through anything else.
function normalizeSide(side: string): string {
  if (side === "BOT") return "BUY";
  if (side === "SLD") return "SELL";
  return side;
}

function rightLabel(right: string | null): string {
  if (right === "C") return "Call";
  if (right === "P") return "Put";
  return right ?? "";
}

// Sign-preserving price tag: credit combos have negative prices and must render
// "-$0.40", not "$-0.40" (web/CLAUDE.md credit/debit convention). Empty when null.
function priceTag(value: number | null): string {
  return value != null ? `@ ${fmtSignedPrice(value)}` : "";
}

// Contract label, shared by orders + fills. Guards nullable OPT strike/right so
// incomplete metadata never renders "$null".
function describeContract(c: OrderContract): string {
  if (c.secType === "BAG") return `${c.symbol} combo`;
  if (c.secType === "OPT") {
    const parts = [c.symbol, rightLabel(c.right)];
    if (c.strike != null) parts.push(`$${c.strike}`);
    return parts.filter(Boolean).join(" ");
  }
  return c.symbol;
}

function describeOrder(o: OpenOrder): string {
  const qty = Math.abs(o.totalQuantity ?? 0);
  return `${o.action || ""} ${qty}× ${describeContract(o.contract)} ${priceTag(o.limitPrice)}`.trim();
}

function describeFill(f: ExecutedOrder): string {
  const qty = Math.abs(f.quantity ?? 0);
  return `${normalizeSide(f.side || "")} ${qty}× ${describeContract(f.contract)} ${priceTag(f.avgPrice)}`.trim();
}

/**
 * OrdersSnapshotCard — compressed "what am I working on right now" view: top-3
 * working orders + top-3 of today's fills, click-through to /orders. IB-only;
 * when handed null (FUTU tab) it renders its empty state.
 */
export function OrdersSnapshotCard({ orders }: Props) {
  const open = (orders?.open_orders ?? []).slice(0, 3);
  const recent = (orders?.executed_orders ?? []).slice(0, 3);
  const hasAny = open.length > 0 || recent.length > 0;

  return (
    <section className="snapshot-card">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">Orders / 02</p>
        <h3 className="panel-title">Working &amp; Filled</h3>
        <Link className="snapshot-card__see-all" href="/orders">
          All orders →
        </Link>
      </header>

      {!hasAny ? (
        <div className="snapshot-card__empty">
          No open or filled orders today.
        </div>
      ) : (
        <div className="snapshot-card__split">
          <div className="snapshot-list">
            <p className="snapshot-list__kicker">
              Working
              <span className="snapshot-list__count">
                {orders?.open_count ?? 0}
              </span>
            </p>
            {open.length === 0 ? (
              <div className="snapshot-list__empty">No working orders.</div>
            ) : (
              <ul className="snapshot-list__items">
                {/* Index key: pending DB orders can have permId AND orderId
                    both coerced to 0 (orders.py _int_or_zero), so an id-based
                    key would collide. This list is a read-only top-3 snapshot
                    with no per-row state, so the index is a safe, stable key. */}
                {open.map((o, i) => (
                  <li key={`o-${i}`} className="snapshot-list__row">
                    <span className="snapshot-list__row-desc">
                      {describeOrder(o)}
                    </span>
                    <span className="snapshot-list__row-meta">
                      {o.status ?? "—"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="snapshot-list">
            <p className="snapshot-list__kicker">
              Today&apos;s Fills
              <span className="snapshot-list__count">
                {orders?.executed_count ?? 0}
              </span>
            </p>
            {recent.length === 0 ? (
              <div className="snapshot-list__empty">No fills today.</div>
            ) : (
              <ul className="snapshot-list__items">
                {recent.map((f, i) => (
                  <li key={`f-${f.execId || i}`} className="snapshot-list__row">
                    <span className="snapshot-list__row-desc">
                      {describeFill(f)}
                    </span>
                    <span className="snapshot-list__row-meta">
                      {fmtTime(f.time)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
