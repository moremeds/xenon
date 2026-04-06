import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, test } from "vitest";
import { ogFamilyContract } from "../lib/og-theme";
import {
  resolveMenthorqRenderer,
  type DashboardCache,
} from "../app/api/menthorq/[command]/image/route";

function renderSelection(data: DashboardCache) {
  const selection = resolveMenthorqRenderer(data.command, data);
  const html = renderToStaticMarkup(
    createElement(selection.Renderer, { data }),
  );

  return { selection, html };
}

test("MenthorQ OG route routes intraday-style payloads to the analytical time-series family", () => {
  const { selection, html } = renderSelection({
    date: "2026-03-11",
    command: "intraday",
    data: [
      { date: "2026-03-10", value: 12.4 },
      { date: "2026-03-11", value: 13.1 },
      { date: "2026-03-12", value: 12.8 },
    ],
    metadata: {},
  });

  expect(selection.family).toEqual(ogFamilyContract("analytical-time-series"));
  expect(selection.componentName).toBe("AnalyticalTimeSeriesChart");
  expect(html).toContain("<svg");
  expect(html).toContain("2026-03-10");
});

test("MenthorQ OG route routes metric payloads to the distribution-bar family", () => {
  const { selection, html } = renderSelection({
    date: "2026-03-11",
    command: "vol",
    data: [
      { metric: "Vol Control", value: 0.85, signal: "risk-on" },
      { metric: "Vol Barometer", value: 0.42, signal: "neutral" },
      { metric: "Skew", value: -0.31, signal: "risk-off" },
    ],
    metadata: {},
  });

  expect(selection.family).toEqual(ogFamilyContract("distribution-bar"));
  expect(selection.componentName).toBe("DistributionBarChart");
  expect(html).toContain("<svg");
  expect(html).toContain("Vol Control");
});

test("MenthorQ OG route routes row-column payloads to the matrix-heatmap family", () => {
  const { selection, html } = renderSelection({
    date: "2026-03-11",
    command: "forex",
    data: [
      { row: "EURUSD", col: "Gamma", value: 1.2 },
      { row: "EURUSD", col: "Blindspot", value: -0.4 },
      { row: "USDJPY", col: "Gamma", value: 0.7 },
      { row: "USDJPY", col: "Blindspot", value: 1.8 },
    ],
    metadata: {},
  });

  expect(selection.family).toEqual(ogFamilyContract("matrix-heatmap"));
  expect(selection.componentName).toBe("MatrixHeatmapChart");
  expect(html).toContain("<svg");
  expect(html).toContain("EURUSD");
  expect(html).toContain("Blindspot");
});

test("MenthorQ OG route keeps command-specific family badges but falls back gracefully for unsupported shapes", () => {
  const { selection, html } = renderSelection({
    date: "2026-03-11",
    command: "vol",
    data: [
      { metric: "Vol Control", payload: { state: "risk-on" } },
      { metric: "Vol Barometer", payload: { state: "neutral" } },
    ] as DashboardCache["data"],
    metadata: {},
  });

  expect(selection.family).toEqual(ogFamilyContract("distribution-bar"));
  expect(selection.componentName).toBe("UnsupportedChart");
  expect(html).toContain("Unsupported");
  expect(html).toContain("VOL");
});
