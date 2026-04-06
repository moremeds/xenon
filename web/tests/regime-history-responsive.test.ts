import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import { join } from "path";
import { fileURLToPath } from "url";

const TEST_DIR = fileURLToPath(new URL(".", import.meta.url));
const PANEL_PATH = join(TEST_DIR, "../components/RegimePanel.tsx");
const CSS_PATH = join(TEST_DIR, "../app/globals.css");
const panelSource = readFileSync(PANEL_PATH, "utf-8");
const cssSource = readFileSync(CSS_PATH, "utf-8");

describe("RegimePanel — responsive history chart layout", () => {
  it("uses a named history-grid class instead of an inline fixed two-column grid", () => {
    expect(panelSource).toContain('className="regime-history-grid"');
    expect(panelSource).toContain('data-testid="regime-history-grid"');
    expect(panelSource).not.toContain('<div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px" }}>'); 
  });

  it("collapses the history grid to a single column below the narrow-screen breakpoint", () => {
    expect(cssSource).toMatch(/\.regime-history-grid\s*\{[\s\S]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
    expect(cssSource).toMatch(/@media\s*\(max-width:\s*960px\)\s*\{[\s\S]*\.regime-history-grid\s*\{[\s\S]*grid-template-columns:\s*1fr;/);
  });
});
