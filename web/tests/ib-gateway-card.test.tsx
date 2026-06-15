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

const POOL = {
  sync: { connected: true, client_id: 1 },
  orders: { connected: true, client_id: 2 },
  data: { connected: false, client_id: 3 },
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
        pool={POOL}
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
        pool={{}}
      />,
    );
    expect(container.querySelector(".operator-pill--fault")).toBeTruthy();
  });
  it("embeds the pool roles as pills with status dots", () => {
    const { container } = render(
      <IbGatewayCard
        gateway={GW}
        verdict="authenticated"
        account="DU***889"
        tradingMode="paper"
        modeVerified
        pool={POOL}
      />,
    );
    // sync/orders/data roles surface as inline pills inside the gateway card.
    expect(screen.getByText("sync")).toBeTruthy();
    expect(screen.getByText("orders")).toBeTruthy();
    expect(screen.getByText("data")).toBeTruthy();
    expect(container.querySelectorAll(".operator-role__dot").length).toBe(3);
    // data is disconnected → down dot.
    expect(container.querySelector(".operator-role__dot--down")).toBeTruthy();
  });
});
