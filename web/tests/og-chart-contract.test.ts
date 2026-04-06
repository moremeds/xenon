import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import { areaChartSvg, barChartSvg, heatmapSvg, lineChartSvg } from "../lib/og-charts";
import { OG } from "../lib/og-theme";

function expectSharedAxisContract(markup: string) {
  expect(markup).toContain(`font-family="${OG.chart.axisFontFamily}"`);
  expect(markup).toContain(`font-size="${OG.chart.axisFontSize}"`);
}

describe("og chart primitives", () => {
  test("line and area charts render axis labels through the shared chart-system contract", () => {
    const data = [
      { label: "Mon", value: 10 },
      { label: "Tue", value: 14 },
      { label: "Wed", value: 12 },
    ];

    expectSharedAxisContract(
      renderToStaticMarkup(lineChartSvg({ data, width: 320, height: 180 })!)
    );
    expectSharedAxisContract(
      renderToStaticMarkup(areaChartSvg({ data, width: 320, height: 180 })!)
    );
  });

  test("bar and heatmap charts render axis labels through the shared chart-system contract", () => {
    const barMarkup = renderToStaticMarkup(
      barChartSvg({
        data: [
          { label: "A", value: 3 },
          { label: "B", value: -1 },
          { label: "C", value: 2 },
        ],
        width: 320,
        height: 180,
      })!
    );
    const heatmapMarkup = renderToStaticMarkup(
      heatmapSvg({
        data: [
          { row: "R1", col: "C1", value: 1 },
          { row: "R1", col: "C2", value: -1 },
          { row: "R2", col: "C1", value: 0.5 },
          { row: "R2", col: "C2", value: -0.5 },
        ],
        rows: ["R1", "R2"],
        cols: ["C1", "C2"],
        width: 320,
        height: 180,
      })!
    );

    expectSharedAxisContract(barMarkup);
    expectSharedAxisContract(heatmapMarkup);
  });
});
