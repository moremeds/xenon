import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";

const SCANNER_MOCK = {
  scan_id: "trend_20260410_0845",
  scan_timestamp: "2026-04-10T08:45:12-04:00",
  market_context: { spy_close: 523.45, vix_close: 18.2, regime: "bullish" },
  universe_size: 743,
  stage_a_survivors: 187,
  stage_b_survivors: 92,
  candidates: [
    {
      ticker: "NVDA",
      snapshot_timestamp: "2026-04-10T08:45:12-04:00",
      spot_price: 148.3,
      direction: "bullish",
      final_score: 0.82,
      scores: { trend: 0.91, structure: 0.75, volatility: 0.68, flow: 0.85 },
      indicators: {
        ma_20: 142.5,
        ma_50: 138.2,
        ma_200: 125.8,
        rsi: 62.3,
        adx: 32.1,
        macd_histogram: 1.45,
        bbw: 0.08,
        rs_vs_spy: 1.15,
        iv_rank: 22,
        gamma_flip: 145,
        call_wall: 160,
        put_wall: 140,
      },
      summaries: {
        trend: "Full MA stack, ADX 32, RS 1.15 vs SPY, breakout flag",
        structure: "Above gamma flip by 2.3%, call wall at +8%, put support at -3%",
        vol: "IV rank 22, normal term structure, IV/RV 0.94",
        flow: "4 ask-side prints, clustered 1-4 week expiry, dark-pool alignment",
      },
      suggested_trade: "debit_call",
      invalidation: 142.5,
      flags: ["event_premium"],
      holding_window: "5-15 trading days",
    },
  ],
};

const PORTFOLIO_MOCK = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: "2026-04-10T12:00:00Z",
  positions: [],
  total_deployed_pct: 0,
  total_deployed_dollars: 0,
  remaining_capacity_pct: 100,
  position_count: 0,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 100_000,
    daily_pnl: 0,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 100_000,
    maintenance_margin: 0,
    excess_liquidity: 100_000,
    buying_power: 200_000,
    dividends: 0,
  },
};

const EMPTY_ORDERS = {
  last_sync: "2026-04-10T12:00:00Z",
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

const CRI_MOCK = {
  scan_time: "2026-04-10T12:00:00Z",
  market_open: true,
  date: "2026-04-10",
  vix: 18.2,
  vvix: 92,
  spy: 523.45,
  vix_5d_roc: -0.8,
  vvix_vix_ratio: 5.0,
  realized_vol: 11.3,
  cor1m: 31.4,
  cor1m_previous_close: 30.7,
  cor1m_5d_change: 0.7,
  spx_100d_ma: 516,
  spx_distance_pct: 1.4,
  spy_closes: Array.from({ length: 22 }, (_, i) => 515 + i * 0.4),
  cri: { score: 14.5, level: "LOW", components: { vix: 4, vvix: 4, correlation: 3, momentum: 3.5 } },
  crash_trigger: {
    triggered: false,
    conditions: { spx_below_100d_ma: false, realized_vol_gt_25: false, cor1m_gt_60: false },
  },
  cta: { exposure_pct: 94, forced_reduction_pct: 0, est_selling_bn: 0 },
  menthorq_cta: null,
  history: [],
};

async function stubScannerPage(page: import("@playwright/test").Page) {
  await page.unrouteAll({ behavior: "ignoreErrors" });

  await page.route("**/api/scanner", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SCANNER_MOCK) }),
  );
  await page.route("**/api/portfolio", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PORTFOLIO_MOCK) }),
  );
  await page.route("**/api/orders", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(EMPTY_ORDERS) }),
  );
  await page.route("**/api/regime", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(CRI_MOCK) }),
  );
  await page.route("**/api/ib-status", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ connected: true }) }),
  );
  await page.route("**/api/blotter", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        as_of: new Date().toISOString(),
        summary: { realized_pnl: 0, closed_trades: 0, open_trades: 0, total_commissions: 0 },
        closed_trades: [],
        open_trades: [],
      }),
    }),
  );
  await page.route("**/api/menthorq/cta", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ tables: [] }) }),
  );
  await page.route("**/api/previous-close", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ closes: {} }) }),
  );
  await page.route("**/api/prices**", (route) => route.abort());

  await page.addInitScript(() => {
    class MockWebSocket {
      public static OPEN = 1;
      public url: string;
      public readyState = 0;
      public onopen: ((event: Event) => void) | null = null;
      public onmessage: ((event: MessageEvent<string>) => void) | null = null;
      public onclose: ((event: Event) => void) | null = null;
      public onerror: ((event: Event) => void) | null = null;

      constructor(url: string) {
        this.url = url;
        window.setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.(new Event("open"));
        }, 0);
      }

      send(_message: string) {}

      close() {
        this.readyState = 3;
        this.onclose?.(new Event("close"));
      }
    }

    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      writable: true,
      value: MockWebSocket,
    });
  });
}

test.describe("Trend scanner page", () => {
  test("renders ranked candidates and expands row details", async ({ page }) => {
    await stubScannerPage(page);
    await page.goto("/scanner");

    await expect(page.getByText("Trend Scanner")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("1 CANDIDATES")).toBeVisible();
    await expect(page.getByRole("button", { name: "View details for NVDA" })).toBeVisible();
    await expect(page.getByText("BULLISH", { exact: true })).toBeVisible();
    await expect(page.getByText("debit call", { exact: true })).toBeVisible();
    await expect(page.getByText("SPY 523 · VIX 18.2 · bullish")).toBeVisible();

    await page.getByRole("button", { name: "View details for NVDA" }).click();
    await expect(page.getByText("Invalidation:")).toBeVisible();
    await expect(page.getByText("Full MA stack, ADX 32, RS 1.15 vs SPY, breakout flag")).toBeVisible();
    await expect(page.getByText("IV rank 22, normal term structure, IV/RV 0.94")).toBeVisible();

    const evidenceDir = join(process.cwd(), "..", "data", "evidence", "scanner");
    mkdirSync(evidenceDir, { recursive: true });
    await page.screenshot({
      path: join(evidenceDir, "trend-scanner-page.png"),
      fullPage: true,
    });
  });
});
