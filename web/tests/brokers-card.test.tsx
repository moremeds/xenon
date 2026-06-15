/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { BrokersCard } from "@/components/operator/BrokersCard";
import type { OperatorData } from "@/lib/operatorTypes";

afterEach(() => cleanup());

const DATA = {
  ib_gateway: {
    port_listening: true,
    host: "127.0.0.1",
    port: 4002,
    gateway_mode: "cloud",
  },
  ib_pool: {
    sync: { connected: true, client_id: 1 },
    orders: { connected: true, client_id: 2 },
    data: { connected: true, client_id: 3 },
  },
  ib_auth: "authenticated",
  trading_mode: "paper",
  account: "DU***8889",
  mode_verified: true,
  futu: {
    configured: true,
    connected: false,
    last_sync_at: null,
    last_sync_age_s: 1145696,
  },
} as unknown as OperatorData;

describe("BrokersCard", () => {
  it("renders IB and Futu as two sections inside a single card", () => {
    const { container } = render(<BrokersCard data={DATA} />);
    // Exactly one outer card panel — IB + Futu are merged, not two panels.
    expect(container.querySelectorAll(".snapshot-card").length).toBe(1);
    expect(container.querySelectorAll(".operator-broker").length).toBe(2);
    // Both broker identities present.
    expect(screen.getByText("IB Gateway")).toBeTruthy();
    expect(screen.getByText("Gateway")).toBeTruthy();
    expect(screen.getByText("Futu")).toBeTruthy();
    expect(screen.getByText("OpenD")).toBeTruthy();
    // 3 IB role dots + 2 Futu role dots = 5.
    expect(container.querySelectorAll(".operator-role__dot").length).toBe(5);
  });
});
