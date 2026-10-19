import { expect, test } from "@playwright/test";

// PR-17: Wizard Risk Alert banner (GET /api/wizard/alerts) and the Operator
// console Flex Token tile (/api/admin/operator `flex_token`). FastAPI is
// stubbed at the Next /api boundary; values are labeled fixtures.

const ALERTS = {
  alerts: [
    {
      session_id: "fixture-crossed",
      ticker: "FIXTURE-A",
      state: "CROSSED",
      threshold: "1.20",
      quote: "1.10",
      quote_as_of: "2026-10-20T15:00:00+00:00",
      evaluated_at: "2026-10-20T15:00:05+00:00",
    },
    {
      session_id: "fixture-noquote",
      ticker: "FIXTURE-B",
      state: "NO_QUOTE",
      threshold: "-0.80",
      quote: null,
      quote_as_of: null,
      evaluated_at: "2026-10-20T15:00:05+00:00",
    },
  ],
  events: [],
  monitor: { running: true, last_run_at: "2026-10-20T15:00:05+00:00" },
};

const OPERATOR = {
  generated_at: "2026-10-20T15:00:00Z",
  ib_gateway: { port_listening: true, host: "h", port: 4002, gateway_mode: "local" },
  ib_pool: { sync: { connected: true, client_id: 11 } },
  ib_auth: "authenticated",
  trading_mode: "paper",
  account: "DU***000",
  mode_verified: true,
  snapshotter: { last_write_at: "2026-10-20T14:59:00Z", stale_seconds: 12 },
  order_submissions: { unknown_count: 0, alarm: false },
  flex_divergence: { configured: true, ran_at: null, divergence_count: 0, total_compared: 0 },
  flex_token: {
    configured: true,
    expires_at: "2026-10-25T00:00:00+00:00",
    days_remaining: 5,
    reminder_days: [30, 14, 7, 1],
    active_threshold: 7,
    should_warn: true,
    expired: false,
  },
  realtime_subscribers: { reachable: true, ib_connected: true, subscribers: [], anonymous_count: 0, ttl_ms: 30000 },
  futu: { configured: false, connected: false, last_sync_at: null, last_sync_age_s: null },
  writers: [],
};

const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

test.beforeEach(async ({ page }) => {
  await page.route("**/api/wizard/alerts", (r) => r.fulfill(json(ALERTS)));
  await page.route("**/api/admin/operator", (r) => r.fulfill(json(OPERATOR)));
  await page.route("**/api/admin/uw-quota", (r) => r.fulfill(json({ configured: false })));
  await page.route("**/api/flex-token", (r) => r.fulfill(json({ should_warn: false })));
  await page.route("**/api/portfolio", (r) =>
    r.fulfill(
      json({
        bankroll: 100_000, peak_value: 100_000, last_sync: "2026-10-20T15:00:00Z",
        total_deployed_pct: 0, total_deployed_dollars: 0, remaining_capacity_pct: 100,
        position_count: 0, defined_risk_count: 0, undefined_risk_count: 0,
        avg_kelly_optimal: null, positions: [], exposure: {}, violations: [], account_summary: {},
      }),
    ),
  );
  await page.route("**/api/orders", (r) =>
    r.fulfill(json({ last_sync: "2026-10-20T15:00:00Z", open_orders: [], executed_orders: [], open_count: 0, executed_count: 0 })),
  );
  await page.route("**/api/ib-status", (r) => r.fulfill(json({ connected: false })));
  await page.route("**/api/blotter", (r) =>
    r.fulfill(json({ as_of: "2026-10-20T15:00:00Z", summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] })),
  );
  await page.route("**/api/previous-close", (r) => r.fulfill(json({ closes: {} })));
  await page.route("**/api/prices**", (r) => r.abort());
});

test("wizard risk alert banner renders CROSSED and NO_QUOTE", async ({ page }) => {
  await page.goto("/portfolio");
  const crossed = page.getByTestId("wizard-risk-alert-fixture-crossed");
  await expect(crossed).toContainText("FIXTURE-A net mid 1.10 crossed alert line 1.20");
  await expect(page.getByTestId("wizard-risk-alert-fixture-noquote")).toContainText("no fresh quote");
  await page.locator(".wizard-risk-alert-banner").screenshot({
    path: "../output/playwright/pr17-wizard-risk-alert-banner-2026-10-20.png",
  });
  await page.screenshot({ path: "../output/playwright/pr17-wizard-risk-alert-page-2026-10-20.png" });
});

test("operator console renders the Flex Token tile", async ({ page }) => {
  await page.goto("/admin");
  const tile = page.locator(".operator-tile", { hasText: "Flex Token" });
  await expect(tile.locator(".operator-tile__value")).toHaveText("5d");
  await expect(tile.locator(".operator-tile__value")).toHaveClass(/operator-tile__value--fault/);
  await page.locator(".operator-surface").screenshot({
    path: "../output/playwright/pr17-operator-flex-token-tile-2026-10-20.png",
  });
});
