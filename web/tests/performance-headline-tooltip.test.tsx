/**
 * @vitest-environment jsdom
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

afterEach(() => cleanup());

import PerformanceFreshness from "@/components/PerformanceFreshness";
import PerformanceHeadlineTooltip from "@/components/PerformanceHeadlineTooltip";

const SUMMARY = {
  total_return: 0.123,
  simple_total_return: 0.123,
  twr_total_return: 0.118,
  irr_total_return: 0.116,
  net_external_flows: 2700,
};

describe("PerformanceHeadlineTooltip", () => {
  it("renders the headline (TWR) value when present, with simple fallback", () => {
    // Component prefers twr_total_return > simple_total_return > total_return.
    render(<PerformanceHeadlineTooltip summary={SUMMARY} currency="USD" />);
    expect(screen.getByTestId("performance-headline").textContent).toContain(
      "+11.80%",
    );
  });

  it("info icon is keyboard-focusable", () => {
    render(<PerformanceHeadlineTooltip summary={SUMMARY} currency="USD" />);
    const icon = screen.getByTestId("performance-headline-info");
    expect(icon.getAttribute("tabindex")).toBe("0");
  });

  it("shows TWR, IRR, and net deposits on hover", () => {
    render(<PerformanceHeadlineTooltip summary={SUMMARY} currency="USD" />);
    fireEvent.mouseEnter(screen.getByTestId("performance-headline-info"));
    const tooltip = screen.getByTestId("performance-headline-tooltip");
    expect(tooltip.textContent).toContain("Time-Weighted");
    expect(tooltip.textContent).toContain("+11.80%");
    expect(tooltip.textContent).toContain("Money-Weighted");
    expect(tooltip.textContent).toContain("+11.60%");
    expect(tooltip.textContent).toContain("Net deposits");
    expect(tooltip.textContent).toContain("+$2,700");
  });

  it("hides tooltip on mouse leave", () => {
    render(<PerformanceHeadlineTooltip summary={SUMMARY} currency="USD" />);
    const info = screen.getByTestId("performance-headline-info");
    fireEvent.mouseEnter(info);
    expect(screen.getByTestId("performance-headline-tooltip")).toBeTruthy();
    fireEvent.mouseLeave(info);
    expect(screen.queryByTestId("performance-headline-tooltip")).toBeNull();
  });

  it("renders dashes for null TWR/IRR without crashing", () => {
    render(
      <PerformanceHeadlineTooltip
        summary={{
          total_return: 0.05,
          simple_total_return: 0.05,
          twr_total_return: null,
          irr_total_return: null,
          net_external_flows: 0,
        }}
        currency="USD"
      />,
    );
    fireEvent.mouseEnter(screen.getByTestId("performance-headline-info"));
    const tooltip = screen.getByTestId("performance-headline-tooltip");
    expect(tooltip.textContent).toContain("---");
  });
});

describe("PerformanceFreshness", () => {
  it("renders 'intraday + close' when both sources present in series", () => {
    const data = {
      status: "ok" as const,
      currency: "USD",
      last_sync: "2026-06-03T17:35:00Z",
      summary: {} as any,
      series: [
        { date: "2026-06-01", equity: 100, source: "close" },
        { date: "2026-06-03", equity: 101, source: "intraday" },
      ],
    } as any;
    render(<PerformanceFreshness data={data} />);
    expect(screen.getByTestId("performance-freshness").textContent).toContain(
      "intraday + close",
    );
  });

  it("renders single source label when only one present", () => {
    const data = {
      status: "ok" as const,
      currency: "USD",
      last_sync: "2026-06-03T15:00:00Z",
      summary: {} as any,
      series: [{ date: "2026-06-03", equity: 100, source: "intraday" }],
    } as any;
    render(<PerformanceFreshness data={data} />);
    expect(screen.getByTestId("performance-freshness").textContent).toContain(
      "Sources: intraday",
    );
  });

  it("returns null when status is not ok", () => {
    const data = {
      status: "insufficient_history",
      reason: "collecting",
    } as any;
    const { container } = render(<PerformanceFreshness data={data} />);
    expect(container.firstChild).toBeNull();
  });
});
