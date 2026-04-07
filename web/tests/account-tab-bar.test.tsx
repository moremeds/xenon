/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";

afterEach(() => cleanup());
import AccountTabBar, { type AccountTabState } from "@/components/AccountTabBar";

const ibState: AccountTabState = {
  label: "IB",
  accountId: "U12345678",
  environment: "real",
  positionCount: 47,
  lastSync: new Date(Date.now() - 12_000).toISOString(),
  netLiquidation: 847231,
  status: "live",
};

const futuState: AccountTabState = {
  label: "FUTU",
  accountId: "281756478831553263",
  environment: "real",
  positionCount: 27,
  lastSync: new Date(Date.now() - 18_000).toISOString(),
  netLiquidation: 147059,
  status: "live",
};

describe("AccountTabBar", () => {
  it("renders both tabs with labels and status", () => {
    const onChange = vi.fn();
    const { getByText, getAllByText } = render(
      <AccountTabBar active="ib" onChange={onChange} ib={ibState} futu={futuState} />,
    );
    expect(getByText("ACCOUNT · IB")).toBeTruthy();
    expect(getByText("ACCOUNT · FUTU")).toBeTruthy();
    expect(getAllByText(/● LIVE/)).toHaveLength(2);
  });

  it("marks the active tab with aria-pressed", () => {
    const onChange = vi.fn();
    const { getByLabelText } = render(
      <AccountTabBar active="futu" onChange={onChange} ib={ibState} futu={futuState} />,
    );
    expect(getByLabelText(/Switch to FUTU account/).getAttribute("aria-pressed")).toBe("true");
    expect(getByLabelText(/Switch to IB account/).getAttribute("aria-pressed")).toBe("false");
  });

  it("fires onChange when clicking the inactive tab", () => {
    const onChange = vi.fn();
    const { getByLabelText } = render(
      <AccountTabBar active="ib" onChange={onChange} ib={ibState} futu={futuState} />,
    );
    fireEvent.click(getByLabelText(/Switch to FUTU account/));
    expect(onChange).toHaveBeenCalledWith("futu");
  });

  it("truncates long account IDs (Futu's 18-digit acc_id)", () => {
    const onChange = vi.fn();
    const { getByText } = render(
      <AccountTabBar active="ib" onChange={onChange} ib={ibState} futu={futuState} />,
    );
    // 281756478831553263 → 28175…3263
    expect(getByText(/28175…3263/)).toBeTruthy();
  });

  it("renders net liq formatted as currency", () => {
    const onChange = vi.fn();
    const { getByText } = render(
      <AccountTabBar active="ib" onChange={onChange} ib={ibState} futu={futuState} />,
    );
    expect(getByText(/net liq \$847,231/)).toBeTruthy();
    expect(getByText(/net liq \$147,059/)).toBeTruthy();
  });

  it("shows DOWN status when status=down", () => {
    const onChange = vi.fn();
    const down: AccountTabState = { ...futuState, status: "down" };
    const { getByText } = render(
      <AccountTabBar active="ib" onChange={onChange} ib={ibState} futu={down} />,
    );
    expect(getByText(/● DOWN/)).toBeTruthy();
  });

  it("shows STALE status when status=stale", () => {
    const onChange = vi.fn();
    const stale: AccountTabState = { ...futuState, status: "stale" };
    const { getByText } = render(
      <AccountTabBar active="ib" onChange={onChange} ib={ibState} futu={stale} />,
    );
    expect(getByText(/● STALE/)).toBeTruthy();
  });

  it("shows NEVER SYNCED status when status=never_synced", () => {
    const onChange = vi.fn();
    const never: AccountTabState = { ...futuState, status: "never_synced" };
    const { getByText } = render(
      <AccountTabBar active="ib" onChange={onChange} ib={ibState} futu={never} />,
    );
    expect(getByText(/● NEVER SYNCED/)).toBeTruthy();
  });

  it("handles null lastSync gracefully", () => {
    const onChange = vi.fn();
    const fresh: AccountTabState = { ...futuState, lastSync: null };
    const { getByText } = render(
      <AccountTabBar active="ib" onChange={onChange} ib={ibState} futu={fresh} />,
    );
    expect(getByText(/never synced/)).toBeTruthy();
  });
});
