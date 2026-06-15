/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { UwQuotaTile } from "@/components/operator/UwQuotaTile";
import { MarketState } from "@/lib/useMarketHours";
import type { UwQuota } from "@/lib/operatorTypes";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const quota = (over: Partial<UwQuota> = {}): UwQuota => ({
  configured: true,
  daily_count: 1234,
  daily_limit: 100000,
  minute_count: 3,
  minute_remaining: 57,
  minute_reset_ms: 42000,
  fetched_at: "2026-06-15T14:00:00Z",
  ...over,
});

describe("UwQuotaTile", () => {
  it("renders daily used/limit + per-minute sub", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => quota() }),
    );
    render(<UwQuotaTile market={MarketState.OPEN} />);
    await waitFor(() => expect(screen.getByText("1.2k / 100k")).toBeTruthy());
    expect(screen.getByText("57/min left")).toBeTruthy();
  });

  it("shows a not-configured state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () =>
          quota({
            configured: false,
            daily_count: null,
            daily_limit: null,
            minute_remaining: null,
          }),
      }),
    );
    render(<UwQuotaTile market={MarketState.CLOSED} />);
    await waitFor(() =>
      expect(screen.getByText("not configured")).toBeTruthy(),
    );
  });

  it("manual refresh button triggers another fetch (ad-hoc query)", async () => {
    const f = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => quota() });
    vi.stubGlobal("fetch", f);
    render(<UwQuotaTile market={MarketState.CLOSED} />);
    await waitFor(() => expect(screen.getByText("1.2k / 100k")).toBeTruthy());
    const before = f.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: /refresh uw quota/i }));
    await waitFor(() => expect(f.mock.calls.length).toBeGreaterThan(before));
  });
});
