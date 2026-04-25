import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const OPTIONS_CHAIN = path.resolve(
  __dirname,
  "../components/ticker-detail/OptionsChainTab.tsx",
);
const ORDER_TAB = path.resolve(
  __dirname,
  "../components/ticker-detail/OrderTab.tsx",
);

describe("wizard launch wiring", () => {
  it("OrderBuilder plans a FastAPI wizard session before opening the modal", () => {
    const src = fs.readFileSync(OPTIONS_CHAIN, "utf-8");
    expect(src).toContain('fetch("/api/wizard/plan"');
    expect(src).toContain("wizardLauncher.launch(json.session_id");
    expect(src).toContain(
      "const submitTargetPrice = Number.isFinite(signedLimitPrice)",
    );
    expect(src).toContain("wizardSession.refresh();");
    expect(src).toContain("const wizardCanSubmit");
    expect(src).toContain("const wizardCanReprice");
    expect(src).toContain("!wizardIsTerminal");
    expect(src).toContain('"PROTECTION_PENDING"');
    expect(src).toContain('"PROTECTED"');
    expect(src).toContain('"PARTIALLY_FILLED"');
    expect(src).toContain("wizardSession.session?.current_attempt_id");
    expect(src).not.toContain("TODO(task-5): call /api/wizard/session");
  });

  it("held combo order form plans a FastAPI wizard session before opening the modal", () => {
    const src = fs.readFileSync(ORDER_TAB, "utf-8");
    expect(src).toContain('fetch("/api/wizard/plan"');
    expect(src).toContain("wizardLauncher.launch(json.session_id");
    expect(src).toContain(
      "const submitTargetPrice = Number.isFinite(parsedPrice)",
    );
    expect(src).toContain("wizardSession.refresh();");
    expect(src).toContain("const wizardCanSubmit");
    expect(src).toContain("const wizardCanReprice");
    expect(src).toContain("!wizardIsTerminal");
    expect(src).toContain('"PROTECTION_PENDING"');
    expect(src).toContain('"PROTECTED"');
    expect(src).toContain('"PARTIALLY_FILLED"');
    expect(src).toContain("wizardSession.session?.current_attempt_id");
    expect(src).not.toContain("TODO(task-5): call /api/wizard/session");
  });
});
