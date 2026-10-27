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

// 1440 wide; tall so the panel screenshot is not clipped by the console scroll area.
test.use({ viewport: { width: 1440, height: 2400 } });

/** Screenshots without the Next dev-mode indicator. */
async function hideDevBadge(page: import("@playwright/test").Page) {
  await page.addStyleTag({ content: "nextjs-portal { display: none !important; }" });
}

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
    // Same rule as web/middleware.ts (this route mock replaces it in the browser).
    if (!(req.headers()["content-type"] ?? "").startsWith("application/json")) {
      return r.fulfill(json({ error: "Forbidden", reason: "content_type" }, 403));
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

test("admin runtime settings: labels, sources, read/edit, pause, typed confirm", async ({ page }) => {
  await page.goto("/admin");
  await hideDevBadge(page);
  const panel = page.getByTestId("runtime-settings");
  await expect(panel.getByTestId("settings-group-safety")).toBeVisible();
  await expect(panel.getByTestId("settings-group-toggle")).toContainText("Quote routing");
  await expect(panel.getByTestId("settings-group-tuning")).toBeVisible();
  const quotes = page.getByTestId("setting-XENON_QUOTES_DEFAULT");
  await expect(quotes.getByTestId("effective")).toHaveText("ib");
  await expect(quotes.getByTestId("source")).toHaveText("live (relay)");
  const cap = page.getByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
  await expect(cap.getByTestId("label")).toHaveText("Futu market-order cap");
  await expect(cap.getByTestId("effective")).toHaveText("10,000 USD");
  await expect(cap.getByTestId("source")).toHaveText("override");
  await expect(panel).not.toContainText("---");
  await panel.screenshot({ path: "../output/playwright/admin-settings-ux-2026-10-27-read.png" });

  const banner = page.getByTestId("orders-paused-banner");
  await expect(banner).toContainText("Orders active");
  await banner.getByRole("button", { name: "Pause orders" }).click();
  await expect(banner).toContainText("Orders paused");

  const audit = page.getByTestId("setting-XENON_AUDIT_CANCEL");
  await audit.getByRole("button", { name: "Edit" }).click();
  await audit.getByRole("switch", { name: "New value" }).click();
  await audit.getByRole("button", { name: "Save" }).click();
  const step = audit.getByTestId("confirm-step");
  await expect(step).toBeVisible();
  await expect(step.getByTestId("confirm-proposal")).toHaveText("Set Naked-short audit: cancel violations to On");
  await expect(step.getByRole("button", { name: "Confirm change" })).toBeDisabled();
  await step.getByLabel("Type XENON_AUDIT_CANCEL to confirm").fill("XENON_AUDIT_CANCEL");
  await expect(step.getByRole("button", { name: "Confirm change" })).toBeEnabled();
  await panel.screenshot({ path: "../output/playwright/admin-settings-ux-2026-10-27-edit.png" });
  await step.getByRole("button", { name: "Confirm change" }).click();
  await expect(audit.getByTestId("editor")).toBeHidden();

  const puts = (page as unknown as { puts: { key: string; confirm?: string }[] }).puts;
  const auditPuts = puts.filter((p) => p.key === "XENON_AUDIT_CANCEL");
  expect(auditPuts.map((p) => p.confirm ?? null)).toEqual([null, "XENON_AUDIT_CANCEL"]);

  // Resume = DELETE, sent as JSON (the mock refuses it otherwise, like the middleware).
  await banner.getByRole("button", { name: "Resume orders" }).click();
  await expect(banner).toContainText("Orders active");

  // A cap above the env value is refused in the browser; nothing is sent.
  const before = puts.length;
  await cap.getByRole("button", { name: "Edit" }).click();
  await expect(cap.getByTestId("editor")).toContainText("Env cap 25,000 USD");
  await cap.getByLabel("New value").fill("25001");
  await cap.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Futu market-order cap: 25001 is above the env cap 25,000 USD")).toBeVisible();
  expect(puts.length).toBe(before);
});

test.describe("narrow screen", () => {
  test.use({ viewport: { width: 390, height: 2400 } });
  test("admin runtime settings render at 390 wide", async ({ page }) => {
    await page.goto("/admin");
  await hideDevBadge(page);
    const panel = page.getByTestId("runtime-settings");
    await expect(page.getByTestId("setting-XENON_FUTU_TUPLES_LIVE")).toContainText("Stock · Limit · Day · RTH");
    const overflow = await panel.evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await panel.screenshot({ path: "../output/playwright/admin-settings-ux-2026-10-27-mobile.png" });
  });
});
