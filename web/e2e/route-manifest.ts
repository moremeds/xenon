/**
 * E2E route coverage manifest.
 *
 * Maps every app route to its Playwright specs and coverage status.
 * Run route-manifest.test.ts to verify no routes are missing.
 */
export const ROUTE_MANIFEST: Record<
  string,
  { specs: string[]; status: "covered" | "partial" | "missing" }
> = {
  "/": {
    specs: [
      "regime-cor1m.spec.ts",
      "regime-cor1m-live-route.spec.ts",
      "regime-cor1m-live-stream.spec.ts",
      "regime-day-change.spec.ts",
      "regime-detail-panels-responsive.spec.ts",
      "regime-history-responsive.spec.ts",
      "regime-history-tooltip.spec.ts",
      "regime-live-index-stream.spec.ts",
      "regime-live-index-streaming.spec.ts",
      "regime-live-stream-values.spec.ts",
      "regime-market-closed-eod.spec.ts",
      "regime-relationship-view.spec.ts",
      "regime-rvol-history.spec.ts",
      "regime-rvol-history-live-cache.spec.ts",
      "regime-rvol-history-live-route.spec.ts",
      "regime-stale-market-open.spec.ts",
      "regime-strip-responsive.spec.ts",
      "regime-vcg-edr-badge.spec.ts",
      "regime-vix-live-badge.spec.ts",
      "regime-close-transition-refresh.spec.ts",
      "regime-closed-refresh.spec.ts",
      "regime-cta-share-pattern.spec.ts",
    ],
    status: "covered",
  },
  "/portfolio": {
    specs: [
      "portfolio-view-toggle.spec.ts",
      "portfolio-leg-row-runtime.spec.ts",
      "portfolio-market-closed.spec.ts",
      "portfolio-same-day-combo-pnl.spec.ts",
      "account-day-move-ib-daily-pnl.spec.ts",
      "account-metric-cards.spec.ts",
      "day-move-ib-daily-pnl.spec.ts",
      "futu-readonly.spec.ts",
    ],
    status: "covered",
  },
  "/orders": {
    specs: [
      "open-order-combo.spec.ts",
      "open-order-single-detail.spec.ts",
      "order-combo.spec.ts",
      "order-cancel-error-propagation.spec.ts",
      "modify-combo-order.spec.ts",
      "modify-order-confirmation.spec.ts",
      "modify-order-resting-limit.spec.ts",
      "modify-order-spread-telemetry.spec.ts",
      "iwm-close-order-summary.spec.ts",
      "wulf-close-order-naked-short.spec.ts",
      "orders-historical-trades-refresh.spec.ts",
      "historical-trades-filter.spec.ts",
    ],
    status: "covered",
  },
  "/uw-analyze": {
    specs: ["uw-analyze.spec.ts", "uw-analyze-closed-market.spec.ts"],
    status: "partial",
  },
  "/scanner": {
    specs: ["trend-scanner.spec.ts"],
    status: "covered",
  },
  "/performance": {
    specs: [
      "performance-page.spec.ts",
      "performance-chart-axes.spec.ts",
      "performance-chart-theme.spec.ts",
      "performance-market-closed.spec.ts",
    ],
    status: "covered",
  },
  "/regime": {
    specs: [],
    status: "missing",
  },
  "/flow-analysis": {
    specs: [],
    status: "missing",
  },
  "/cta": {
    specs: ["cta-page.spec.ts", "cta-stale-banner.spec.ts"],
    status: "covered",
  },
  "/discover": {
    specs: [],
    status: "missing",
  },
  "/journal": {
    specs: [],
    status: "missing",
  },
  "/internals": {
    specs: ["internals-market-closed.spec.ts"],
    status: "partial",
  },
  "/kit": {
    specs: [],
    status: "missing",
  },
  "/[ticker]": {
    specs: [
      "ticker-page.spec.ts",
      "ticker-search-chain.spec.ts",
      "ticker-search-live.spec.ts",
      "chain-held-leg-prices.spec.ts",
      "chain-sticky-header.spec.ts",
      "pltr-chain-position-focus.spec.ts",
      "crox-bull-call-stale-price.spec.ts",
      "iwm-ticker-detail-combo-sign.spec.ts",
      "iwm-synthetic-mark-label.spec.ts",
      "order-ticket-quote-telemetry.spec.ts",
      "price-bar-quote-telemetry.spec.ts",
      "price-chart-theme.spec.ts",
      "spread-price-bar.spec.ts",
      "risk-reversal-midprice.spec.ts",
      "ilf-chart-price.spec.ts",
    ],
    status: "covered",
  },
  "/dashboard": {
    specs: [],
    status: "missing",
  },
};
