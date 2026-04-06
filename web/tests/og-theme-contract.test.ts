import { describe, expect, test } from "vitest";
import chartSystemSpec from "../lib/chart-system-spec.json";
import { OG, ogFamilyContract, ogSeriesColor, ogSeriesFill, pctileBg } from "../lib/og-theme";

describe("OG chart-system adoption", () => {
  test("maps semantic colors and frame tokens from the shared chart-system spec", () => {
    expect(OG.positive).toBe(chartSystemSpec.seriesRoles.primary.fallback);
    expect(OG.comparison).toBe(chartSystemSpec.seriesRoles.comparison.fallback);
    expect(OG.chart.radius).toBe(chartSystemSpec.surface.radiusPx);
    expect(OG.chart.padding).toBe(chartSystemSpec.surface.paddingPx);
    expect(OG.chart.headerHeight).toBe(chartSystemSpec.surface.headerHeightPx);
    expect(OG.chart.axisFontSize).toBe(chartSystemSpec.axis.fontSizePx);
  });

  test("exposes analytical time-series metadata for downstream OG routes", () => {
    expect(ogFamilyContract("analytical-time-series")).toEqual({
      id: "analytical-time-series",
      label: chartSystemSpec.families["analytical-time-series"].label,
      renderer: chartSystemSpec.families["analytical-time-series"].renderer,
      interaction: chartSystemSpec.families["analytical-time-series"].interaction,
      requiresAxes: chartSystemSpec.families["analytical-time-series"].requiresAxes,
      rendererDescription: chartSystemSpec.sanctionedRenderers.svg,
    });
  });

  test("uses semantic fills instead of one-off hardcoded percentile colors", () => {
    expect(ogSeriesColor("dislocation")).toBe(chartSystemSpec.seriesRoles.dislocation.fallback);
    expect(ogSeriesFill("primary", 0.12)).toBe("rgba(5, 173, 152, 0.12)");
    expect(pctileBg(80)).toBe("rgba(5, 173, 152, 0.25)");
    expect(pctileBg(15)).toBe("rgba(232, 93, 108, 0.12)");
  });
});
