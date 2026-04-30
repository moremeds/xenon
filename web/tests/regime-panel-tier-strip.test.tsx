// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { RegimeTierStrip } from "../components/RegimeTierStrip";

afterEach(() => {
  cleanup();
});

describe("RegimeTierStrip — per-scanner tier strip", () => {
  it("renders both VCG-R and CRI tiers with binding-side highlight on VCG", () => {
    render(
      <RegimeTierStrip
        data={{
          vcg_tier: "TIER_2",
          cri_tier: "NORMAL",
          binding_tier: "TIER_2",
          binding_side: "vcg",
          vcg_scanned_at: "2026-04-29T15:00:00Z",
          cri_scanned_at: "2026-04-29T15:00:00Z",
          is_stale: false,
          panic_active: false,
        }}
      />,
    );

    const vcg = screen.getByTestId("regime-tier-vcg");
    const cri = screen.getByTestId("regime-tier-cri");
    expect(vcg.textContent).toContain("TIER_2");
    expect(cri.textContent).toContain("NORMAL");
    expect(vcg.getAttribute("data-binding")).toBe("true");
    expect(cri.getAttribute("data-binding")).toBe("false");
  });

  it("highlights both badges when binding_side is 'both'", () => {
    render(
      <RegimeTierStrip
        data={{
          vcg_tier: "TIER_2",
          cri_tier: "TIER_2",
          binding_tier: "TIER_2",
          binding_side: "both",
          vcg_scanned_at: "2026-04-29T15:00:00Z",
          cri_scanned_at: "2026-04-29T15:00:00Z",
          is_stale: false,
          panic_active: false,
        }}
      />,
    );
    expect(
      screen.getByTestId("regime-tier-vcg").getAttribute("data-binding"),
    ).toBe("true");
    expect(
      screen.getByTestId("regime-tier-cri").getAttribute("data-binding"),
    ).toBe("true");
  });

  it("renders stale-data banner when is_stale is true", () => {
    render(
      <RegimeTierStrip
        data={{
          vcg_tier: "UNKNOWN",
          cri_tier: "NORMAL",
          binding_tier: "EDR",
          binding_side: "both",
          vcg_scanned_at: "2026-04-29T13:00:00Z",
          cri_scanned_at: "2026-04-29T15:00:00Z",
          is_stale: true,
          panic_active: false,
        }}
      />,
    );
    expect(screen.getByText(/regime data stale/i)).toBeTruthy();
  });

  it("does not render stale banner when is_stale is false", () => {
    render(
      <RegimeTierStrip
        data={{
          vcg_tier: "NORMAL",
          cri_tier: "NORMAL",
          binding_tier: "NORMAL",
          binding_side: "none",
          vcg_scanned_at: "2026-04-29T15:00:00Z",
          cri_scanned_at: "2026-04-29T15:00:00Z",
          is_stale: false,
          panic_active: false,
        }}
      />,
    );
    expect(screen.queryByText(/regime data stale/i)).toBeNull();
  });

  it("renders panic banner when panic_active is true", () => {
    render(
      <RegimeTierStrip
        data={{
          vcg_tier: "PANIC",
          cri_tier: "TIER_1",
          binding_tier: "PANIC",
          binding_side: "vcg",
          vcg_scanned_at: "2026-04-29T15:00:00Z",
          cri_scanned_at: "2026-04-29T15:00:00Z",
          is_stale: false,
          panic_active: true,
        }}
      />,
    );
    expect(screen.getByTestId("regime-panic-banner")).toBeTruthy();
    expect(screen.getByTestId("regime-panic-banner").textContent).toMatch(
      /panic/i,
    );
  });

  it("renders nothing when data is null", () => {
    const { container } = render(<RegimeTierStrip data={null} />);
    expect(container.firstChild).toBeNull();
  });
});
