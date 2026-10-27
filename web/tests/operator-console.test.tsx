/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import OperatorConsole from "@/components/operator/OperatorConsole";
import type { OperatorData } from "@/lib/operatorTypes";
import { MarketState } from "@/lib/useMarketHours";

// Pin the market state so tone assertions never depend on wall-clock time.
const market = vi.hoisted(() => ({ state: "open" }));
vi.mock("@/lib/useMarketHours", async (orig) => ({
  ...(await orig<typeof import("@/lib/useMarketHours")>()),
  useMarketHours: () => market.state,
}));

afterEach(() => {
  market.state = MarketState.OPEN;
  cleanup();
  vi.restoreAllMocks();
});

const DATA: OperatorData = {
  generated_at: "2026-06-15T14:00:00Z",
  ib_gateway: {
    port_listening: true,
    host: "h",
    port: 4001,
    gateway_mode: "cloud",
  },
  ib_pool: { sync: { connected: true, client_id: 11 } },
  ib_auth: "authenticated",
  trading_mode: "paper",
  account: "DU***889",
  mode_verified: true,
  snapshotter: { last_write_at: "2026-06-15T13:59:00Z", stale_seconds: 12 },
  order_submissions: { unknown_count: 0, alarm: false },
  flex_divergence: {
    configured: true,
    ran_at: null,
    divergence_count: 0,
    total_compared: 0,
  },
  realtime_subscribers: {
    reachable: true,
    ib_connected: true,
    subscribers: [],
    anonymous_count: 0,
    ttl_ms: 30000,
  },
  futu: {
    configured: true,
    connected: false,
    last_sync_at: null,
    last_sync_age_s: null,
  },
  writers: [
    {
      service: "ib_activity_poller",
      state: "ok",
      detail: null,
      last_error: null,
      last_started_at: null,
      last_finished_at: null,
      updated_at: "2026-06-15T13:59:30Z",
      age_secs: 30,
    },
  ],
};

describe("OperatorConsole", () => {
  it("renders tiles + writer table from a fetch", async () => {
    // OperatorConsole fetches /api/admin/operator; UwQuotaTile fetches
    // /api/admin/uw-quota — route the mock by URL.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) =>
        Promise.resolve({
          ok: true,
          json: async () =>
            String(url).includes("/uw-quota")
              ? {
                  configured: true,
                  daily_count: 12,
                  daily_limit: 100000,
                  minute_count: 1,
                  minute_remaining: 119,
                  minute_reset_ms: 1000,
                  fetched_at: "x",
                }
              : DATA,
        }),
      ),
    );
    render(<OperatorConsole />);
    await waitFor(() =>
      expect(screen.getByText(/ib_activity_poller/)).toBeTruthy(),
    );
    expect(screen.getByText("IB Gateway")).toBeTruthy();
  });
  it.each([
    [undefined, "---", "neutral"],
    [{ configured: false }, "---", "neutral"],
    [
      {
        configured: true,
        expires_at: "2027-03-01T00:00:00+00:00",
        days_remaining: 132,
        active_threshold: null,
        should_warn: false,
        expired: false,
      },
      "132d",
      "core",
    ],
    [
      {
        configured: true,
        expires_at: "2026-11-10T00:00:00+00:00",
        days_remaining: 20,
        active_threshold: 30,
        should_warn: true,
        expired: false,
      },
      "20d",
      "warn",
    ],
    [
      {
        configured: true,
        expires_at: "2026-10-25T00:00:00+00:00",
        days_remaining: 5,
        active_threshold: 7,
        should_warn: true,
        expired: false,
      },
      "5d",
      "fault",
    ],
    [
      {
        configured: true,
        expires_at: "2026-10-01T00:00:00+00:00",
        days_remaining: -19,
        active_threshold: 1,
        should_warn: true,
        expired: true,
      },
      "expired",
      "fault",
    ],
  ])("renders the Flex Token tile (%#)", async (flexToken, value, tone) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) =>
        Promise.resolve({
          ok: true,
          json: async () =>
            String(url).includes("/uw-quota")
              ? { configured: false }
              : { ...DATA, flex_token: flexToken },
        }),
      ),
    );
    render(<OperatorConsole />);
    const label = await screen.findByText("Flex Token");
    const tile = label.closest(".operator-tile") as HTMLElement;
    const valueEl = tile.querySelector(".operator-tile__value") as HTMLElement;
    expect(valueEl.textContent).toBe(value);
    expect(valueEl.className).toContain(`operator-tile__value--${tone}`);
  });

  it("does not count the gate4 watermark sentinel as a writer", async () => {
    const withSentinel: OperatorData = {
      ...DATA,
      writers: [
        ...DATA.writers,
        {
          service: "gate4_view_invalidated",
          state: "invalidated",
          detail: '{"reason": "unprovenanced_open_orders", "seq": "1"}',
          last_error: null,
          last_started_at: null,
          last_finished_at: "2026-06-15T10:00:00Z",
          updated_at: "2026-06-15T10:00:00Z",
          age_secs: 14400,
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) =>
        Promise.resolve({
          ok: true,
          json: async () =>
            String(url).includes("/uw-quota")
              ? { configured: false }
              : withSentinel,
        }),
      ),
    );
    render(<OperatorConsole />);
    await waitFor(() =>
      expect(screen.getByText(/ib_activity_poller/)).toBeTruthy(),
    );
    expect(screen.queryByText("gate4_view_invalidated")).toBeNull();
    expect(screen.getByText(/1\/1 healthy/)).toBeTruthy();
  });
  it("shows a loading state before data", () => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));
    render(<OperatorConsole />);
    expect(screen.getByText(/operator — loading/i)).toBeTruthy();
  });
  it("surfaces a fault instead of hanging when the feed errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue({ ok: false, status: 502, json: async () => ({}) }),
    );
    render(<OperatorConsole />);
    await waitFor(() => expect(screen.getByText(/HTTP 502/)).toBeTruthy());
  });

  function stubOperator(payload: OperatorData) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) =>
        Promise.resolve({
          ok: true,
          json: async () =>
            String(url).includes("/uw-quota") ? { configured: false } : payload,
        }),
      ),
    );
  }
  async function snapshotterValue() {
    const label = await screen.findByText("Snapshotter");
    const tile = label.closest(".operator-tile") as HTMLElement;
    return tile.querySelector(".operator-tile__value") as HTMLElement;
  }

  it("faults a stale snapshotter while the market is open", async () => {
    stubOperator({
      ...DATA,
      snapshotter: { last_write_at: null, stale_seconds: 6510 },
    });
    render(<OperatorConsole />);
    expect((await snapshotterValue()).className).toContain(
      "operator-tile__value--fault",
    );
  });
  it("does not fault a stale snapshotter while the market is closed", async () => {
    market.state = MarketState.CLOSED;
    stubOperator({
      ...DATA,
      snapshotter: { last_write_at: null, stale_seconds: 6510 },
    });
    render(<OperatorConsole />);
    const value = await snapshotterValue();
    expect(value.className).toContain("operator-tile__value--neutral");
    expect(value.className).not.toContain("--fault");
  });
  it("flags an authenticated gateway with a down pool role in the rollup", async () => {
    stubOperator({
      ...DATA,
      ib_pool: {
        sync: { connected: true, client_id: 3 },
        orders: { connected: true, client_id: 4 },
        data: { connected: false, client_id: 5 },
      },
    });
    const { container } = render(<OperatorConsole />);
    await screen.findByText("Snapshotter");
    const rollup = container.querySelector(".operator-rollup") as HTMLElement;
    expect(rollup.textContent).toMatch(/IB authenticated.*data down/);
  });
});
