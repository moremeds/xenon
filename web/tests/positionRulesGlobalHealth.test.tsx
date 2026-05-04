/**
 * @vitest-environment jsdom
 */

import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GlobalHealthIndicator } from "@/components/portfolio/GlobalHealthIndicator";
import * as api from "@/lib/api/positionRules";

vi.mock("@/lib/api/positionRules", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/positionRules")>();
  return {
    ...actual,
    fetchHealth: vi.fn(),
  };
});

function makeHealth(overrides: Partial<api.PositionRulesHealth> = {}): api.PositionRulesHealth {
  return {
    schema_version: 1,
    daemon_alive: true,
    market_window: "open",
    next_market_event_at: "2026-05-04T20:00:00Z",
    last_tick_at: "2026-05-04T14:00:00Z",
    last_tick_age_seconds: 30,
    rule_counts_by_state: {
      PENDING_ARM: 0,
      ARMED: 5,
      TRIGGERED: 0,
      CLOSED: 0,
      CANCELED: 0,
      FAILED: 0,
      SUPERSEDED: 0,
    },
    claim_counts_by_status: {
      PENDING: 0,
      SUBMITTED: 0,
      FILLED: 0,
      FAILED: 0,
      ABANDONED: 0,
    },
    in_flight_claims: 0,
    stale_quote_skips_last_hour: 0,
    unprotected_position_count: 0,
    ib_connected: true,
    outbox_dlq_count: 0,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("GlobalHealthIndicator", () => {
  it("is green when everything is healthy during open market", async () => {
    vi.mocked(api.fetchHealth).mockResolvedValue(makeHealth());
    const { container } = render(<GlobalHealthIndicator />);

    await waitFor(() => expect(container.querySelector("[data-cls=green]")).toBeTruthy());
    expect(container.textContent).toContain("5 armed");
  });

  it("does not mark stale ticks red outside RTH", async () => {
    vi.mocked(api.fetchHealth).mockResolvedValue(
      makeHealth({
        market_window: "closed",
        last_tick_age_seconds: 7200,
      }),
    );
    const { container } = render(<GlobalHealthIndicator />);

    await waitFor(() => expect(container.querySelector("[data-cls=green]")).toBeTruthy());
  });

  it("is red when DLQ has rows", async () => {
    vi.mocked(api.fetchHealth).mockResolvedValue(makeHealth({ outbox_dlq_count: 1 }));
    const { container } = render(<GlobalHealthIndicator />);

    await waitFor(() => expect(container.querySelector("[data-cls=red]")).toBeTruthy());
  });

  it("is amber when claims are in flight", async () => {
    vi.mocked(api.fetchHealth).mockResolvedValue(makeHealth({ in_flight_claims: 2 }));
    const { container } = render(<GlobalHealthIndicator />);

    await waitFor(() => expect(container.querySelector("[data-cls=amber]")).toBeTruthy());
  });
});
