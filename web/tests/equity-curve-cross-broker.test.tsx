/**
 * @vitest-environment jsdom
 */
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { EquityCurveCrossBroker } from "@/components/EquityCurveCrossBroker";

afterEach(cleanup);

beforeEach(() => {
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      per_broker: {
        IB: [{ date: "2024-05-01", nav: 2000, source: "reported" }],
        FUTU: [{ date: "2024-05-01", nav: 1000, source: "reconstructed" }],
      },
      combined: null,
    }),
  });
});

describe("<EquityCurveCrossBroker />", () => {
  it("renders two-line chart", async () => {
    render(<EquityCurveCrossBroker />);
    expect(await screen.findByTestId("equity-curve-cross-broker")).toBeTruthy();
  });

  it("toggle Combined NAV refetches with combined=true", async () => {
    render(<EquityCurveCrossBroker />);
    const toggle = await screen.findByLabelText(/combined/i);
    fireEvent.click(toggle);
    expect(fetch).toHaveBeenLastCalledWith(
      expect.stringContaining("combined=true"),
    );
  });
});
