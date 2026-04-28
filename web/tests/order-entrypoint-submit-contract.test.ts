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
  ])("%s includes client_attempt_id on /api/orders/place submissions", (file) => {
    const source = readFileSync(resolve(root, file), "utf8");
    expect(source).toContain('fetch("/api/orders/place"');
    expect(source).toContain("client_attempt_id");
  });
});
