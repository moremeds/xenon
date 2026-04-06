import { readFileSync } from "fs";
import { resolve } from "path";
import { expect, test } from "vitest";

const css = readFileSync(resolve(__dirname, "../app/globals.css"), "utf-8");

test("[theme] performance chart background uses the dedicated theme variable", () => {
  expect(css).toMatch(/--performance-chart-bg:/);
  expect(css).toMatch(/\.performance-chart\s*\{[\s\S]*background:\s*var\(--performance-chart-bg\)/);
});

test("[theme] performance chart grid and benchmark line use dedicated theme variables", () => {
  expect(css).toMatch(/--performance-chart-grid:/);
  expect(css).toMatch(/--performance-chart-benchmark:/);
  expect(css).toMatch(/\.performance-grid-line\s*\{[\s\S]*stroke:\s*var\(--performance-chart-grid\)/);
  expect(css).toMatch(/\.performance-line-benchmark\s*\{[\s\S]*stroke:\s*var\(--performance-chart-benchmark\)/);
});

test("[theme] performance chart meta tiles use the dedicated theme variable", () => {
  expect(css).toMatch(/--performance-chart-meta-bg:/);
  expect(css).toMatch(/\.performance-meta-item\s*\{[\s\S]*background:\s*var\(--performance-chart-meta-bg\)/);
});
