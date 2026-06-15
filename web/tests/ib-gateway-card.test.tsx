/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { IbGatewayCard } from "@/components/operator/IbGatewayCard";

afterEach(() => cleanup());

const GW = {
  port_listening: true,
  host: "100.66.147.98",
  port: 4001,
  gateway_mode: "cloud",
};

describe("IbGatewayCard", () => {
  it("shows host:port and the auth verdict", () => {
    render(
      <IbGatewayCard
        gateway={GW}
        verdict="authenticated"
        account="DU***889"
        tradingMode="paper"
        modeVerified
      />,
    );
    expect(screen.getByText(/100\.66\.147\.98:4001/)).toBeTruthy();
    expect(screen.getByText(/authenticated/i)).toBeTruthy();
  });
  it("marks an unreachable gateway with the fault pill tone", () => {
    const { container } = render(
      <IbGatewayCard
        gateway={{ port_listening: false }}
        verdict="unreachable"
        account=""
        tradingMode="paper"
        modeVerified={false}
      />,
    );
    expect(container.querySelector(".operator-pill--fault")).toBeTruthy();
  });
});
