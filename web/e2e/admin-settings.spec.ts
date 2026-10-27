import { expect, test, type Route } from "@playwright/test";

import { settingsFixture } from "../tests/fixtures/runtimeSettings";

// Runtime settings section on /admin. FastAPI is stubbed at the Next /api
// boundary (page.route); values are labeled fixtures, not live settings.

const OPERATOR = {
  generated_at: "2026-10-27T15:00:00Z",
  ib_gateway: { port_listening: true, host: "h", port: 4002, gateway_mode: "local" },
  ib_pool: { sync: { connected: true, client_id: 103 } },
  ib_auth: "authenticated",
  trading_mode: "paper",
  account: "DU***000",
  mode_verified: true,
  snapshotter: { last_write_at: "2026-10-27T14:59:00Z", stale_seconds: 12 },
  order_submissions: { unknown_count: 0, alarm: false },
  flex_divergence: { configured: true, ran_at: null, divergence_count: 0, total_compared: 0 },
  flex_token: { configured: false },
  realtime_subscribers: { reachable: true, ib_connected: true, subscribers: [], anonymous_count: 0, ttl_ms: 30000 },
  futu: { configured: false, connected: false, last_sync_at: null, last_sync_age_s: null },
  writers: [],
};

const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });

// Tall viewport so the panel screenshot is not clipped by the console scroll area.
test.use({ viewport: { width: 1440, height: 2400 } });

test.beforeEach(async ({ page }) => {
  let paused = false;
  const puts: unknown[] = [];
  (page as unknown as { puts: unknown[] }).puts = puts;
  await page.route("**/api/admin/operator", (r) => r.fulfill(json(OPERATOR)));
  await page.route("**/api/admin/uw-quota", (r) => r.fulfill(json({ configured: false })));
  await page.route("**/api/admin/settings**", async (r: Route) => {
    const req = r.request();
    const method = req.method();
    if (method === "GET") {
      const rows = settingsFixture().map((s) =>
        s.key === "orders_paused" ? { ...s, db_value: paused ? true : null, effective: paused } : s,
      );
      return r.fulfill(json({ settings: rows }));
    }
    const key = new URL(req.url()).pathname.split("/").pop();
    if (method === "PUT") {
      const body = req.postDataJSON() as { value: unknown; confirm?: string };
      puts.push({ key, ...body });
      if (key === "XENON_AUDIT_CANCEL" && body.confirm !== "XENON_AUDIT_CANCEL") {
        return r.fulfill(json({ reason_code: "SETTING_CONFIRM_REQUIRED", detail: "needs confirm" }, 428));
      }
      if (key === "orders_paused") paused = body.value === true;
      return r.fulfill(json({ key, changed: true }));
    }
    if (method === "DELETE") {
      if (key === "orders_paused") paused = false;
      return r.fulfill(json({ key, changed: true }));
    }
    return r.fulfill(json({}, 405));
  });
  await page.route("**/api/wizard/alerts", (r) => r.fulfill(json({ alerts: [], events: [], monitor: { running: false } })));
  await page.route("**/api/flex-token", (r) => r.fulfill(json({ should_warn: false })));
  await page.route("**/api/portfolio", (r) =>
    r.fulfill(
      json({
        bankroll: 100_000, peak_value: 100_000, last_sync: "2026-10-27T15:00:00Z",
        total_deployed_pct: 0, total_deployed_dollars: 0, remaining_capacity_pct: 100,
        position_count: 0, defined_risk_count: 0, undefined_risk_count: 0,
        avg_kelly_optimal: null, positions: [], exposure: {}, violations: [], account_summary: {},
      }),
    ),
  );
  await page.route("**/api/orders", (r) =>
    r.fulfill(json({ last_sync: "2026-10-27T15:00:00Z", open_orders: [], executed_orders: [], open_count: 0, executed_count: 0 })),
  );
  await page.route("**/api/ib-status", (r) => r.fulfill(json({ connected: false })));
  await page.route("**/api/blotter", (r) =>
    r.fulfill(json({ as_of: "2026-10-27T15:00:00Z", summary: { realized_pnl: 0 }, closed_trades: [], open_trades: [] })),
  );
  await page.route("**/api/previous-close", (r) => r.fulfill(json({ closes: {} })));
  await page.route("**/api/prices**", (r) => r.abort());
});

test("admin runtime settings: groups, owner env, pause toggle, typed confirm", async ({ page }) => {
  await page.goto("/admin");
  const panel = page.getByTestId("runtime-settings");
  await expect(panel.getByTestId("settings-group-safety")).toBeVisible();
  await expect(panel.getByTestId("settings-group-toggle")).toBeVisible();
  await expect(panel.getByTestId("settings-group-tuning")).toBeVisible();
  await expect(page.getByTestId("setting-XENON_QUOTES_DEFAULT").getByTestId("env")).toHaveText("env: set in relay");
  const cap = page.getByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
  await expect(cap.getByTestId("effective")).toHaveText("10000");
  await expect(cap.getByTestId("env")).toHaveText("25000");

  const banner = page.getByTestId("orders-paused-banner");
  await expect(banner).toContainText("Orders active");
  await banner.getByRole("button", { name: "Pause orders" }).click();
  await expect(banner).toContainText("Orders paused");

  const audit = page.getByTestId("setting-XENON_AUDIT_CANCEL");
  await audit.getByLabel("New value").selectOption("true");
  await audit.getByRole("button", { name: "Save" }).click();
  const step = audit.getByTestId("confirm-step");
  await expect(step).toBeVisible();
  await expect(step.getByRole("button", { name: "Confirm change" })).toBeDisabled();
  await step.getByLabel("Type XENON_AUDIT_CANCEL to confirm").fill("XENON_AUDIT_CANCEL");
  await expect(step.getByRole("button", { name: "Confirm change" })).toBeEnabled();
  await panel.screenshot({ path: "../output/playwright/admin-settings-2026-10-27.png" });
  await step.getByRole("button", { name: "Confirm change" }).click();
  await expect(step).toBeHidden();

  const puts = (page as unknown as { puts: { key: string; confirm?: string }[] }).puts;
  const auditPuts = puts.filter((p) => p.key === "XENON_AUDIT_CANCEL");
  expect(auditPuts.map((p) => p.confirm ?? null)).toEqual([null, "XENON_AUDIT_CANCEL"]);
});
