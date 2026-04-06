import { readFileSync } from "fs";
import { resolve } from "path";
import { expect, test } from "vitest";

const panelSource = readFileSync(resolve(__dirname, "../components/PerformancePanel.tsx"), "utf-8");
const css = readFileSync(resolve(__dirname, "../app/globals.css"), "utf-8");

test("[axes] performance chart renders dedicated x and y axis groups", () => {
  expect(panelSource).toContain('data-testid="performance-y-axis"');
  expect(panelSource).toContain('data-testid="performance-x-axis"');
});

test("[axes] performance chart renders axis labels with mono styling hooks", () => {
  expect(panelSource).toContain("performance-axis-label");
  expect(css).toMatch(/\.performance-axis-label\s*\{/);
  expect(css).toMatch(/font-family:\s*var\(--font-mono\)/);
});
