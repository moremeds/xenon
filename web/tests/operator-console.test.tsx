/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import OperatorConsole from "@/components/operator/OperatorConsole";
import type { OperatorData } from "@/lib/operatorTypes";

afterEach(() => {
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
});
