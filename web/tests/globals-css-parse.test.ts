import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import postcss from "postcss";

// Regression guard: globals.css is only compiled by turbopack at runtime, so a
// CSS syntax error (e.g. a stray "*/" inside a comment prematurely closing it,
// leaking the rest as invalid CSS) passes typecheck + lint + the logic tests
// and only breaks when the dev/prod server builds the page. This pins a plain
// postcss parse so the whole stylesheet must be syntactically valid in CI.
describe("app/globals.css", () => {
  it("parses as valid CSS (no unterminated comments / stray tokens)", () => {
    const __dirname = resolve(fileURLToPath(import.meta.url), "..");
    const cssPath = resolve(__dirname, "..", "app", "globals.css");
    const css = readFileSync(cssPath, "utf8");
    expect(() => postcss.parse(css, { from: cssPath })).not.toThrow();
  });
});
