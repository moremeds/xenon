/**
 * E2E: /uw-analyze closed-market gate behavior.
 *
 * Locks in the refresh-button bypass path added in the silly-humming-tide.md
 * plan. The automatic portfolio GET must pass through the backend gate
 * untouched (no `?user_initiated` query), while the refresh button must
 * trigger POST /uw-analyze/refresh AND chain a follow-up SSE GET carrying
 * `?user_initiated=1` so the backend lifts the gate and runs the analyser.
 *
 * Verifies:
 *  1. Top-strip header copy switches to "auto-refresh paused (click refresh)"
 *     when the backend response has market_state=closed.
 *  2. The open-state copy "auto-refresh 2m" is NOT rendered in closed state.
 *  3. Initial portfolio GETs (cached + SSE) do NOT include user_initiated=1.
 *  4. Clicking ↻ REFRESH ALL fires POST /uw-analyze/refresh.
 *  5. The follow-up portfolio GET after the refresh click carries
 *     `?user_initiated=1` — the backend bypass signal.
 *
 * Regex routes are used instead of glob patterns so the matcher is
 * unambiguous about query-string URLs (the frontend hits both
 * `?cached=true` and `?user_initiated=1` variants against the same path).
 */

import { test, expect, type Page } from "@playwright/test";

const PORTFOLIO_RE = /\/api\/uw-analyze\/portfolio(\?|$)/;
const REFRESH_RE = /\/api\/uw-analyze\/refresh(\?|$)/;

const CLOSED_PORTFOLIO_BODY = JSON.stringify({
  // Old fetched_at — simulates "Friday close served on Saturday"
  fetched_at: "2026-04-10T20:00:00Z",
  response_generated_at: "2026-04-12T14:00:00Z",
  market_state: "closed",
  ttl_seconds: 3600,
  closed_market_paused: true,
  tickers: [],
  action_items: [],
});

async function mockClosedPortfolio(page: Page): Promise<void> {
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // Portfolio GET — matches cached, SSE, and user_initiated variants.
  await page.route(PORTFOLIO_RE, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: CLOSED_PORTFOLIO_BODY,
    });
  });

  // Refresh POST — stub a no-op success. The real upstream would re-run
  // the analyser; for this spec we only care that the frontend calls it.
  await page.route(REFRESH_RE, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ refreshed: 0, failed: [] }),
    });
  });
}

test.describe("UW Analysis — closed-market gate", () => {
  test("header copy shows 'auto-refresh paused' when market_state=closed", async ({
    page,
  }) => {
    await mockClosedPortfolio(page);
    await page.goto("/uw-analyze");

    const strip = page.getByTestId("uw-analyze-top-strip");
    await expect(strip).toContainText("auto-refresh paused (click refresh)");
    // The open-state copy must not appear.
    await expect(strip).not.toContainText("auto-refresh 2m");
  });

  test("refresh button triggers POST /uw-analyze/refresh and chains GET with user_initiated=1", async ({
    page,
  }) => {
    await mockClosedPortfolio(page);

    const portfolioGets: string[] = [];
    let refreshPosted = false;

    // Track every request the frontend makes so we can assert on the
    // exact sequence and query-string of follow-up portfolio GETs.
    page.on("request", (req) => {
      const url = req.url();
      if (req.method() === "GET" && /\/api\/uw-analyze\/portfolio/.test(url)) {
        portfolioGets.push(url);
      }
      if (req.method() === "POST" && /\/api\/uw-analyze\/refresh/.test(url)) {
        refreshPosted = true;
      }
    });

    await page.goto("/uw-analyze");
    await expect(page.getByTestId("uw-analyze-top-strip")).toContainText(
      "auto-refresh paused",
    );

    // Initial load: cached + SSE GETs may fire — NONE may carry
    // user_initiated=1, that's the whole point of the gate.
    const initialCount = portfolioGets.length;
    for (const url of portfolioGets) {
      expect(
        url,
        `initial GET should not bypass closed-market gate: ${url}`,
      ).not.toContain("user_initiated=1");
    }

    // Click ↻ REFRESH ALL.
    await page.getByTestId("uw-analyze-refresh-all").click();

    // Wait for the follow-up GET to fire (max 5s).
    await expect
      .poll(() => portfolioGets.length, { timeout: 5000 })
      .toBeGreaterThan(initialCount);

    // POST /uw-analyze/refresh must have been issued.
    expect(refreshPosted, "refresh button must POST /uw-analyze/refresh").toBe(
      true,
    );

    // At least one GET fired AFTER the click must carry user_initiated=1 —
    // this is the backend bypass signal set by refreshAll() in
    // useUwPortfolio.ts. Without it, the SSE GET hits the gated path
    // and the backend returns last-known-good stale snapshots, defeating
    // the user's refresh action.
    const postRefreshGets = portfolioGets.slice(initialCount);
    expect(
      postRefreshGets.some((u) => u.includes("user_initiated=1")),
      `follow-up GET must bypass gate via ?user_initiated=1; got: ${postRefreshGets.join(", ")}`,
    ).toBe(true);
  });
});
