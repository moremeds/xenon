/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { FutuGatewayCard } from "@/components/operator/FutuGatewayCard";
import type { FutuInfo } from "@/lib/operatorTypes";

afterEach(() => cleanup());

const base: FutuInfo = {
  configured: true,
  connected: true,
  last_sync_at: "2026-06-15T13:59:00Z",
  last_sync_age_s: 42,
};

describe("FutuGatewayCard", () => {
  it("shows sync as live and orders as not-supported (read-only)", () => {
    const { container } = render(<FutuGatewayCard futu={base} />);
    expect(screen.getByText("Futu")).toBeTruthy();
    expect(screen.getByText("sync")).toBeTruthy();
    expect(screen.getByText("orders")).toBeTruthy();
    // sync connected → ok dot; orders unsupported → off pill, never a live dot.
    expect(container.querySelector(".operator-role__dot--ok")).toBeTruthy();
    expect(container.querySelector(".operator-role--off")).toBeTruthy();
    expect(container.querySelector(".operator-role__dot--off")).toBeTruthy();
  });

  it("renders an idle state when configured but disconnected", () => {
    const { container } = render(
      <FutuGatewayCard
        futu={{
          configured: true,
          connected: false,
          last_sync_at: null,
          last_sync_age_s: null,
        }}
      />,
    );
    expect(screen.getByText(/idle/i)).toBeTruthy();
    // sync is down when OpenD is unreachable.
    expect(container.querySelector(".operator-role__dot--down")).toBeTruthy();
  });

  it("renders an off state when Futu is not configured", () => {
    render(
      <FutuGatewayCard
        futu={{
          configured: false,
          connected: false,
          last_sync_at: null,
          last_sync_age_s: null,
        }}
      />,
    );
    expect(screen.getByText(/^off$/i)).toBeTruthy();
  });
});
