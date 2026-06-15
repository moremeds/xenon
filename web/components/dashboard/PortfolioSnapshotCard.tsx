"use client";

import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { fmtUsd, fmtSignedUsd } from "@/lib/format";
import { resolveAccountDayPnlValue } from "@/components/MetricCards";

type Props = {
  portfolio: PortfolioData | null;
  prices?: Record<string, PriceData>;
};

function pnlTone(value: number | null): "core" | "fault" | "neutral" {
  if (value == null || value === 0) return "neutral";
  return value > 0 ? "core" : "fault";
}

const money = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? "---" : fmtUsd(n);

const moneySigned = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? "---" : fmtSignedUsd(n);

/**
 * PortfolioSnapshotCard — top-of-dashboard account summary: Net Liquidation,
 * Today P&L, Open Risk (deployed capital), free cash. Reads the portfolio prop
 * already hydrated by WorkspaceShell — no new data plumbing.
 *
 * Today P&L is broker-aware via resolveAccountDayPnlValue(): IB's streamed
 * daily_pnl for IB, intraday-from-live-prices for FUTU. (Credit/debit sign
 * preserved — no Math.abs on P&L.)
 */
export function PortfolioSnapshotCard({ portfolio, prices }: Props) {
  const acct = portfolio?.account_summary;
  const netLiq = acct?.net_liquidation ?? null;
  const todayPnl = portfolio
    ? resolveAccountDayPnlValue(portfolio, prices)
    : null;
  const cash = acct?.cash ?? acct?.settled_cash ?? null;
  const openRisk = portfolio?.total_deployed_dollars ?? null;
  const todayTone = pnlTone(todayPnl);

  return (
    <section className="snapshot-card">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">Portfolio / 01</p>
        <h3 className="panel-title">Account</h3>
      </header>
      <div className="snapshot-grid snapshot-grid--portfolio">
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Net Liquidation</span>
          <span className="snapshot-cell__value">{money(netLiq)}</span>
        </div>
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Today P&amp;L</span>
          <span
            className={`snapshot-cell__value snapshot-cell__value--${todayTone}`}
          >
            {moneySigned(todayPnl)}
          </span>
        </div>
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Open Risk</span>
          <span className="snapshot-cell__value">{money(openRisk)}</span>
        </div>
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Cash</span>
          <span className="snapshot-cell__value">{money(cash)}</span>
        </div>
      </div>
    </section>
  );
}
