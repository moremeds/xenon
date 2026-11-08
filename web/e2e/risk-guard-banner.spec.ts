import { expect, test } from "@playwright/test";

// Tail-risk guard banner on the workspace shell. FastAPI is stubbed at the Next
// /api boundary (page.route); prices and account values are labeled fixtures.

const json = (body: unknown, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

const STATE = {
  running: true,
  enabled: true,
  tier: "FAST",
  incidents: [
    {
      key: "market",
      incident_id: "market#1",
      severity: "FAST",
      reasons: ["30m -1.60%", "level broken 6950"],
      acked: false,
      clearing: false,
    },
    {
      key: "health:SOURCE_STALE",
      incident_id: "h#2",
      severity: "BLIND",
      reasons: ["no tick: SPY 41 s"],
      acked: false,
      clearing: false,
    },
  ],
  sources: {
    SPX: { last: 6888.0, age_s: 1, fresh: true },
    SPY: { last: 688.1, age_s: 41, fresh: false },
  },
  account: {
    nlv: 250000,
    cushion: 0.1834,
    leverage: 0.82,
    width_ratio: 1.1,
    drawdown: -0.061,
    drawdown_adjusted: true,
    snapshot_age_s: 31,
  },
  events: [],
};

test.use({ viewport: { width: 1440, height: 900 } });

test("risk guard banner shows incidents, details and acks one", async ({
  page,
}) => {
  let acked = false;
  const contentTypes: string[] = [];
  await page.route("**/api/risk/state", (r) =>
    r.fulfill(
      json({
        ...STATE,
        incidents: STATE.incidents.map((i) =>
          i.key === "market" ? { ...i, acked } : i,
        ),
      }),
    ),
  );
  await page.route("**/api/risk/incidents/*/ack", (r) => {
    contentTypes.push(r.request().headers()["content-type"] ?? "");
    acked = true;
    return r.fulfill(json({ key: "market", acked: true }));
  });

  await page.goto("/portfolio");
  await page.addStyleTag({
    content: "nextjs-portal { display: none !important; }",
  });
  const banner = page.getByTestId("risk-guard-banner");
  await expect(banner).toBeVisible();
  await expect(page.getByTestId("risk-incident-market")).toContainText(
    "30m -1.60%",
  );
  await expect(
    page.getByTestId("risk-incident-health:SOURCE_STALE"),
  ).toContainText("BLIND · SOURCE_STALE");
  await banner.getByText("DETAILS").click();
  await expect(page.getByTestId("risk-guard-panel")).toContainText(
    "SPY 688.1 · tick 41 s · STALE",
  );
  await banner.screenshot({
    path: "../output/playwright/risk-guard-2026-11-08.png",
  });

  await page.getByTestId("risk-incident-market").getByText("ACK").click();
  await expect(page.getByTestId("risk-incident-market")).toContainText("ACKED");
  expect(contentTypes).toEqual(["application/json"]);
  await banner.screenshot({
    path: "../output/playwright/risk-guard-2026-11-08-acked.png",
  });
});
