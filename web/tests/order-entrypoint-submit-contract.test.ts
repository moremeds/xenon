import { readFileSync } from "fs";
import { resolve } from "path";
import { describe, expect, it } from "vitest";

const root = resolve(__dirname, "../..");

describe("order entrypoints submit contract", () => {
  it.each([
    "web/components/ticker-detail/OptionsChainTab.tsx",
    "web/components/ticker-detail/OrderTab.tsx",
    "web/components/PositionOrderModal.tsx",
    "web/components/InstrumentDetailModal.tsx",
    "web/components/ticker-detail/BookTab.tsx",
  ])(
    "%s includes client_attempt_id on /api/orders/place submissions",
    (file) => {
      // PR-07: every form places through the one submitPlaceOrder entry, which
      // stamps client_attempt_id and refuses a resubmit after an uncertain send.
      const source = readFileSync(resolve(root, file), "utf8");
      expect(source).toMatch(/submitPlaceOrder\(\s*attemptId,/);
      expect(source).not.toMatch(/fetch\(["']\/api\/orders\/place["']/);
      expect(source).toContain("attemptId.uncertain");
    },
  );

  it("submitPlaceOrder stamps client_attempt_id on /api/orders/place", () => {
    const source = readFileSync(
      resolve(root, "web/lib/order/submitPlace.ts"),
      "utf8",
    );
    expect(source).toContain("/api/orders/place");
    expect(source).toContain("client_attempt_id: attempt.id");
  });

  it("the replacement client_attempt_id is owned by OrderActionsContext, not the modal mount", () => {
    const modal = readFileSync(
      resolve(root, "web/components/ModifyOrderModal.tsx"),
      "utf8",
    );
    const ctx = readFileSync(
      resolve(root, "web/lib/OrderActionsContext.tsx"),
      "utf8",
    );
    expect(modal).not.toContain("randomAttemptId");
    expect(modal).not.toContain("client_attempt_id");
    expect(ctx).toMatch(/replaceOrder:[\s\S]*client_attempt_id:\s*attemptId/);
  });
});
