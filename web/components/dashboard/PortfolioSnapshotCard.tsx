"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import type { FutuStalenessState } from "@/lib/futuStaleness";
import { fmtUsd, fmtUsdRound, fmtSignedUsd } from "@/lib/format";
import { accountMetrics, mergeAccountMetrics } from "@/lib/accountMerge";

export type DashboardAccount = {
  source: "ib" | "futu";
  label: string;
  accountId: string | null;
  status: FutuStalenessState;
  portfolio: PortfolioData | null;
};

type Props = {
  accounts: DashboardAccount[];
  prices?: Record<string, PriceData>;
};

function pnlTone(value: number | null): "core" | "fault" | "neutral" {
  if (value == null || value === 0) return "neutral";
  return value > 0 ? "core" : "fault";
}

const money = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? "---" : fmtUsd(n);

const moneyFull = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? "---" : fmtUsdRound(n);

const moneySigned = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? "---" : fmtSignedUsd(n);

/**
 * PortfolioSnapshotCard — account summary for the dashboard. By default it shows
 * the MERGED totals across every account (IB + FUTU); the header toggle expands
 * a per-account breakdown. Today P&L is broker-aware (IB streamed daily_pnl,
 * FUTU intraday-from-prices) and sign-preserving — merge math lives in
 * lib/accountMerge.ts.
 */
export function PortfolioSnapshotCard({ accounts, prices }: Props) {
  const [open, setOpen] = useState(false);

  const perAccount = accounts.map((acct) => ({
    acct,
    metrics: accountMetrics(acct.portfolio, prices),
  }));
  const merged = mergeAccountMetrics(perAccount.map((p) => p.metrics));
  const todayTone = pnlTone(merged.todayPnl);
  const mergedLabel = accounts.map((a) => a.label).join(" + ") || "Account";

  return (
    <section className="snapshot-card">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">Portfolio / 01</p>
        <h3 className="panel-title">Account</h3>
        <button
          type="button"
          className="snapshot-card__breakdown-toggle"
          aria-expanded={open}
          aria-controls="portfolio-breakdown"
          aria-label={`Toggle account breakdown (${mergedLabel})`}
          onClick={() => setOpen((prev) => !prev)}
        >
          <span>{mergedLabel}</span>
          {open ? (
            <ChevronDown size={14} aria-hidden />
          ) : (
            <ChevronRight size={14} aria-hidden />
          )}
        </button>
      </header>

      <div className="snapshot-grid snapshot-grid--portfolio">
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Net Liquidation</span>
          <span className="snapshot-cell__value">{money(merged.netLiq)}</span>
        </div>
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Today P&amp;L</span>
          <span
            className={`snapshot-cell__value snapshot-cell__value--${todayTone}`}
          >
            {moneySigned(merged.todayPnl)}
          </span>
        </div>
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Open Risk</span>
          <span className="snapshot-cell__value">{money(merged.openRisk)}</span>
        </div>
        <div className="snapshot-cell">
          <span className="snapshot-cell__label">Cash</span>
          <span className="snapshot-cell__value">{money(merged.cash)}</span>
        </div>
      </div>

      <div
        id="portfolio-breakdown"
        className="snapshot-breakdown"
        hidden={!open}
      >
        {perAccount.map(({ acct, metrics }) => {
          const pos =
            acct.portfolio?.position_count ??
            acct.portfolio?.positions.length ??
            0;
          return (
            <div key={acct.source} className="snapshot-breakdown__row">
              <span className="snapshot-breakdown__head">
                <span
                  className={`snapshot-breakdown__dot snapshot-breakdown__dot--${acct.status}`}
                  aria-hidden
                />
                <span className="snapshot-breakdown__label">{acct.label}</span>
                <span className="snapshot-breakdown__meta">{pos} pos</span>
              </span>
              <span className="snapshot-breakdown__metrics">
                <span className="snapshot-breakdown__netliq">
                  {moneyFull(metrics.netLiq)}
                </span>
                <span
                  className={`snapshot-breakdown__pnl snapshot-breakdown__pnl--${pnlTone(
                    metrics.todayPnl,
                  )}`}
                >
                  {moneySigned(metrics.todayPnl)}
                </span>
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}
