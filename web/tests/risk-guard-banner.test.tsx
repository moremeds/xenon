/** @vitest-environment jsdom */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import RiskGuardBanner from "@/components/risk/RiskGuardBanner";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const state = {
  running: true,
  enabled: true,
  tier: "FAST",
  incidents: [
    {
      key: "market",
      incident_id: "market#1",
      severity: "FAST",
      reasons: ["30m -1.60%"],
      acked: false,
      clearing: false,
    },
    {
      key: "health:FEED_STALE",
      incident_id: "h#2",
      severity: "BLIND",
      reasons: ["no tick: SPX 25 s"],
      acked: true,
      clearing: false,
    },
  ],
  sources: {
    SPX: { last: 6888.0, age_s: 1.2, fresh: true },
    SPY: { last: null, age_s: null, fresh: false },
  },
  account: {
    nlv: 123456.7,
    cushion: 0.1834,
    leverage: 0.82,
    width_ratio: 1.1,
    drawdown: -0.061,
    drawdown_adjusted: false,
    snapshot_age_s: 31,
  },
};

function stub(
  responses: Record<string, { ok: boolean; status?: number; body: unknown }>,
) {
  const fetchMock = vi.fn(async (url: string) => {
    const r = responses[url] ?? responses["*"];
    return { ok: r.ok, status: r.status ?? 200, json: async () => r.body };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("RiskGuardBanner", () => {
  it("renders market and BLIND incidents, details panel, and acks with a JSON content type", async () => {
    const fetchMock = stub({
      "/api/risk/state": { ok: true, body: state },
      "/api/risk/incidents/market/ack": {
        ok: true,
        body: { key: "market", acked: true },
      },
    });
    render(<RiskGuardBanner />);
    const market = await screen.findByTestId("risk-incident-market");
    expect(market.className).toContain("risk-guard-incident--fast");
    expect(market.textContent).toContain("30m -1.60%");
    const blind = screen.getByTestId("risk-incident-health:FEED_STALE");
    expect(blind.className).toContain("risk-guard-incident--blind");
    expect(blind.textContent).toContain("BLIND · FEED_STALE");
    expect(blind.textContent).toContain("ACKED");
    expect(screen.getByRole("alert").textContent).toContain(
      "No automatic protection",
    );

    fireEvent.click(screen.getByText("DETAILS"));
    const panel = screen.getByTestId("risk-guard-panel");
    expect(panel.textContent).toContain("SPY --- · tick never · STALE");
    expect(panel.textContent).toContain("NLV $123,457");
    expect(panel.textContent).toContain("Cushion 18.34%");
    expect(panel.textContent).toContain("Leverage 0.82x");
    expect(panel.textContent).toContain("Drawdown -6.10% (unadjusted)");

    fireEvent.click(screen.getByText("ACK"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/api/risk/incidents/market/ack", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      }),
    );
  });

  it("shows the reason_code when an ack is refused", async () => {
    stub({
      "/api/risk/state": {
        ok: true,
        body: { ...state, incidents: [state.incidents[0]] },
      },
      "/api/risk/incidents/market/ack": {
        ok: false,
        status: 503,
        body: {
          reason_code: "RISK_ACK_UNAVAILABLE",
          detail: "ack not recorded (database); alerts continue",
        },
      },
    });
    render(<RiskGuardBanner />);
    fireEvent.click(await screen.findByText("ACK"));
    expect(
      await screen.findByText(/Ack failed: RISK_ACK_UNAVAILABLE/),
    ).toBeTruthy();
  });

  it("labels an extra-index incident with its symbol", async () => {
    stub({
      "/api/risk/state": {
        ok: true,
        body: {
          ...state,
          incidents: [
            {
              ...state.incidents[0],
              key: "market:NDX",
              reasons: ["30m -1.70%"],
            },
          ],
        },
      },
    });
    render(<RiskGuardBanner />);
    const row = await screen.findByTestId("risk-incident-market:NDX");
    expect(row.textContent).toContain("FAST · NDX");
    expect(row.className).toContain("risk-guard-incident--fast");
  });

  it("renders nothing with no incident, when not running, or when the route fails", async () => {
    for (const r of [
      { ok: true, body: { ...state, incidents: [] } },
      { ok: true, body: { running: false } },
      { ok: false, status: 502, body: { detail: "down" } },
    ]) {
      const fetchMock = stub({ "*": r });
      const { container, unmount } = render(<RiskGuardBanner />);
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      expect(container.innerHTML).toBe("");
      unmount();
    }
  });
});
