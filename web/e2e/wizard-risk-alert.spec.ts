import { test, expect } from "@playwright/test";

/**
 * Spec §9.2 — the Risk Alert popup must say "Risk Alert → Assisted Exit".
 * It must NOT say "stop-loss". This is a copy-gate regression.
 *
 * Marked `test.fixme` because Task 4 did not necessarily land the popup UI.
 * TODO: un-fixme once `WizardRiskAlertPopup` is wired into the session
 *       stream and the route/component path below is known.
 */

test.fixme("Risk Alert popup says Assisted Exit, never stop-loss", async ({
  page,
}) => {
  // TODO: Replace these placeholders once the popup component ships:
  //   - the host page route (probably the ticker page or an OrderBuilder modal)
  //   - a way to seed a PROTECTED wizard session with a crossed threshold
  //   - the testid / role used by the popup container
  await page.goto("/"); // TODO: real entry point
  const popup = page.getByTestId("wizard-risk-alert-popup");
  await expect(popup).toBeVisible();
  const text = (await popup.textContent()) || "";
  expect(text.toLowerCase()).toContain("risk alert");
  expect(text.toLowerCase()).toContain("assisted exit");
  expect(text.toLowerCase()).not.toContain("stop-loss");
  expect(text.toLowerCase()).not.toContain("stop loss");
});
