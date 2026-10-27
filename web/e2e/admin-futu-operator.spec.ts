import { expect, test } from "@playwright/test";

// Operator console (/admin): Futu scopes + held rows, quotes line, IB pool
// degraded signal, writer last_error. /api/admin/operator is stubbed at the
// Next /api boundary with the shape of the real prod payload captured
// 2026-10-27 (abbreviated, order ids verbatim from that capture).

const NO_HELD = {
  FUTU_FILL_UNPRICED: { count: 0, order_ids: [] },
  FUTU_FILL_CONFLICT: { count: 0, order_ids: [] },
  FUTU_NOT_OBSERVED: { count: 0, order_ids: [] },
};

const OPERATOR = {
  generated_at: "2026-10-27T07:22:13Z",
  ib_gateway: { port_listening: true, host: "100.66.147.98", port: 4001, gateway_mode: "cloud" },
  ib_pool: {
    sync: { connected: true, broker_readiness: "ready", client_id: 3 },
    orders: { connected: true, broker_readiness: "ready", client_id: 4 },
    data: { connected: false, broker_readiness: "unknown", client_id: 5 },
  },
  ib_auth: "authenticated",
  trading_mode: "live",
  account: "U***31",
  mode_verified: true,
  snapshotter: { last_write_at: "2026-10-27T05:34:23Z", stale_seconds: 6510 },
  order_submissions: { unknown_count: 0, alarm: false },
  flex_divergence: { configured: true, ran_at: null, divergence_count: 0, total_compared: 0 },
  realtime_subscribers: {
    reachable: true,
    ib_connected: true,
    subscribers: [],
    anonymous_count: 5,
    ttl_ms: 900000,
    // Labeled fixture: futu_quotes is the new backend field, not yet in prod.
    futu_quotes: {
      connected: true,
      opend_connected: false,
      quota: { ceiling: 800, own_used: 24, all_used: null, remain: null },
      routes: { futu: 0, ib: 17 },
    },
  },
  futu: {
    configured: true,
    connected: false,
    last_sync_at: "2026-10-27T00:01:25.822Z",
    last_sync_age_s: 26488.4,
    observation: { as_of: "2026-10-27T07:22:13Z", complete: true },
    held_rows: NO_HELD,
    scope_env: "paper",
    trading: { paper: true, live: false },
    capabilities: {
      paper: { tuples: ["OPT:LMT:DAY:RTH", "STK:LMT:DAY:RTH", "STK:MKT:DAY:RTH"], modify: ["LMT"] },
      live: {
        tuples: ["OPT:LMT:DAY:RTH", "OPT:LMT:GTC:RTH", "STK:LMT:DAY:RTH", "STK:LMT:GTC:RTH"],
        modify: ["LMT"],
      },
    },
    scopes: [
      {
        account_env: "paper",
        configured: true,
        last_sync_at: "2026-10-27T00:01:25.822Z",
        last_sync_age_s: 26488.7,
        observation: { as_of: "2026-10-27T07:22:13Z", complete: true },
        scope_observation_loop: true,
        held_rows: NO_HELD,
      },
      {
        account_env: "live",
        configured: true,
        last_sync_at: "2026-10-27T00:01:05.648Z",
        last_sync_age_s: 26509.2,
        observation: { as_of: "2026-10-27T07:22:13Z", complete: true },
        scope_observation_loop: true,
        held_rows: {
          ...NO_HELD,
          FUTU_NOT_OBSERVED: {
            count: 3,
            order_ids: ["FH1D55EEF9EE2D9000", "FH1D55EEFA738C0000", "FH1D55EEFAFB07D000"],
          },
        },
      },
    ],
  },
  writers: [
    {
      service: "futu_history",
      state: "error",
      detail: null,
      last_error: '{"msg": "OpenD unreachable at 127.0.0.1:11111: [Errno 111] Connection refused"}',
      last_started_at: null,
      last_finished_at: null,
      updated_at: "2026-10-26T20:30:00Z",
      age_secs: 39175,
    },
    {
      service: "ib_fills_replay",
      state: "error",
      detail: null,
      last_error: '{"msg": "timed out after 30s"}',
      last_started_at: null,
      last_finished_at: null,
      updated_at: "2026-10-26T20:30:00Z",
      age_secs: 39175,
    },
  ],
};

const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

test("operator console surfaces Futu held rows, quotes, IB pool degradation and writer errors", async ({ page }) => {
  await page.route("**/api/admin/operator", (r) => r.fulfill(json(OPERATOR)));
  await page.route("**/api/admin/uw-quota", (r) => r.fulfill(json({ configured: false })));
  await page.route("**/api/**", (r) => (r.request().url().includes("/api/admin/") ? r.fallback() : r.fulfill(json({}))));

  await page.goto("/admin");
  const surface = page.locator(".operator-surface");
  await expect(surface).toBeVisible();

  // Futu: live scope warns with 3 not-observed rows and their order ids.
  const live = page.locator('[data-scope="live"]');
  await expect(live.locator(".operator-pill--warn")).toHaveText("3 not observed");
  await expect(live).toContainText("FH1D55EEF9EE2D9000");
  await expect(page.locator('[data-scope="paper"]')).toContainText("orders on");
  await expect(live).toContainText("orders off");
  await expect(page.getByText(/OpenD down · futu 0 \/ ib 17 · quota 24\/800/)).toBeVisible();

  // IB: authenticated but data role down is a warn signal, not just a dot.
  await expect(page.locator(".operator-rollup")).toContainText("IB authenticated · data down");
  await expect(page.locator(".operator-broker .operator-pill--warn").first()).toContainText("data down");

  // Writers: parsed last_error text.
  await expect(page.locator(".operator-writers__err").first()).toContainText("OpenD unreachable");

  await surface.screenshot({ path: "../output/playwright/admin-futu-2026-10-27.png" });
  await page.locator(".operator-writers").screenshot({
    path: "../output/playwright/admin-futu-writers-2026-10-27.png",
  });
});
