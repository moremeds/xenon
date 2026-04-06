import { readFileSync } from "fs";
import { resolve } from "path";
import { expect, test } from "vitest";
import chartSystemSpec from "../lib/chart-system-spec.json";
import { OG, ogFamilyContract, ogSeriesColor } from "../lib/og-theme";

const ogChartsSource = readFileSync(
  resolve(__dirname, "../lib/og-charts.tsx"),
  "utf-8",
);
const menthorqOgRouteSource = readFileSync(
  resolve(__dirname, "../app/api/menthorq/[command]/image/route.tsx"),
  "utf-8",
);

test("[og] shared OG theme mirrors the chart-system spec", () => {
  expect(OG.chart.radius).toBe(chartSystemSpec.surface.radiusPx);
  expect(OG.chart.padding).toBe(chartSystemSpec.surface.paddingPx);
  expect(OG.chart.axisFontFamily).toBe(chartSystemSpec.axis.fontFamily);
  expect(ogSeriesColor("primary")).toBe(chartSystemSpec.seriesRoles.primary.fallback);

  const family = ogFamilyContract("analytical-time-series");
  expect(family.renderer).toBe("svg");
  expect(family.requiresAxes).toBe(true);
  expect(family.rendererDescription).toBe(chartSystemSpec.sanctionedRenderers.svg);
});

test("[og] chart primitives use shared OG chart typography and semantic heatmap coloring", () => {
  expect(ogChartsSource).toContain("OG.chart.axisFontFamily");
  expect(ogChartsSource).toContain("OG.chart.axisFontSize");
  expect(ogChartsSource).toContain("ogHeatmapColor");
});

test("[og] menthorq OG route renders with the shared chart typography contract", () => {
  expect(menthorqOgRouteSource).toContain("fontFamily: OG.chart.axisFontFamily");
});
