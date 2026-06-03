/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

afterEach(() => cleanup());

import PerformancePeriodSelector from "@/components/PerformancePeriodSelector";

describe("PerformancePeriodSelector", () => {
  it("renders 4 buttons in fixed order", () => {
    render(<PerformancePeriodSelector value="YTD" onChange={() => {}} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual([
      "1M",
      "3M",
      "YTD",
      "All",
    ]);
  });

  it("marks the selected button as active via aria-pressed", () => {
    render(<PerformancePeriodSelector value="3M" onChange={() => {}} />);
    expect(
      screen.getByRole("button", { name: "3M" }).getAttribute("aria-pressed"),
    ).toBe("true");
    expect(
      screen.getByRole("button", { name: "YTD" }).getAttribute("aria-pressed"),
    ).toBe("false");
  });

  it("calls onChange with the clicked period", () => {
    const onChange = vi.fn();
    render(<PerformancePeriodSelector value="YTD" onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "1M" }));
    expect(onChange).toHaveBeenCalledWith("1M");
  });

  it("does not re-fire onChange when clicking the active period", () => {
    const onChange = vi.fn();
    render(<PerformancePeriodSelector value="YTD" onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "YTD" }));
    expect(onChange).not.toHaveBeenCalled();
  });
});
