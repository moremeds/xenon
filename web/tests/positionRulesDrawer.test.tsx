/**
 * @vitest-environment jsdom
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PositionRulesDrawer } from "@/components/portfolio/PositionRulesDrawer";
import * as api from "@/lib/api/positionRules";

vi.mock("@/lib/api/positionRules", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/positionRules")>();
  return {
    ...actual,
    cancelRule: vi.fn(),
    fetchPositionRules: vi.fn(),
  };
});

function makeRule(overrides: Partial<api.PositionRule> = {}): api.PositionRule {
  return {
    protection_id: 101,
    position_key: "STK::AAPL",
    rule_kind: "stop_loss",
    state: "ARMED",
    asset_class: "stock",
    config: { threshold_pct: -0.08 },
    state_data: {},
    position_descriptor: {},
    native_order_perm_id: 1234,
    armed_at: "2026-05-04T14:00:00Z",
    triggered_at: null,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("PositionRulesDrawer", () => {
  it("filters rules by position key", async () => {
    vi.mocked(api.fetchPositionRules).mockResolvedValue([
      makeRule(),
      makeRule({ protection_id: 202, position_key: "STK::MSFT", rule_kind: "trailing_tp" }),
    ]);

    render(<PositionRulesDrawer positionKey="STK::AAPL" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText("stop_loss")).toBeTruthy());
    expect(screen.queryByText("trailing_tp")).toBeNull();
  });

  it("cancels a rule and refreshes the filtered rows", async () => {
    vi.mocked(api.fetchPositionRules)
      .mockResolvedValueOnce([makeRule()])
      .mockResolvedValueOnce([makeRule({ state: "CANCELED", native_order_perm_id: null })]);
    vi.mocked(api.cancelRule).mockResolvedValue({ protection_id: 101, state: "CANCELED" });

    render(<PositionRulesDrawer positionKey="STK::AAPL" onClose={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: "Cancel rule" }));

    await waitFor(() => expect(api.cancelRule).toHaveBeenCalledWith(101));
    await waitFor(() => expect(screen.getByText("CANCELED")).toBeTruthy());
  });
});
