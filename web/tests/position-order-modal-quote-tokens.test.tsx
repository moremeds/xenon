/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/react";
import PositionOrderModal from "@/components/PositionOrderModal";
import type { PortfolioPosition } from "@/lib/types";

vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="modal">{children}</div>
  ),
}));

vi.mock("@/components/ticker-detail/useClientAttemptId", () => ({
  useClientAttemptId: () => ({
    id: "test-attempt-id-123",
    markSubmitted: vi.fn(),
    markTerminal: vi.fn(),
    onFieldEdit: vi.fn(),
  }),
}));

const comboPosition: PortfolioPosition = {
  id: 1,
  ticker: "SPY",
  structure: "Bull Call Spread",
  structure_type: "vertical_call_debit",
  risk_profile: "defined_risk",
  expiry: "2026-05-16",
  contracts: 1,
  direction: "LONG",
  entry_cost: 300,
  max_risk: 300,
  market_value: 320,
  legs: [
    {
      conId: 111,
      direction: "LONG",
      contracts: 1,
      type: "Call",
      strike: 500,
      entry_cost: 500,
      avg_cost: 5.0,
      market_price: 5.2,
      market_value: 520,
    } as any,
    {
      conId: 222,
      direction: "SHORT",
      contracts: 1,
      type: "Call",
      strike: 510,
      entry_cost: -200,
      avg_cost: 2.0,
      market_price: 2.0,
      market_value: -200,
    } as any,
  ],
  ib_daily_pnl: null,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-04-01",
};

afterEach(() => {
  cleanup();
});

describe("PositionOrderModal — quote tokens", () => {
  beforeEach(() => {
    (global as any).fetch = vi.fn(
      async (url: RequestInfo, _init?: RequestInit) => {
        const u = String(url);
        if (u.startsWith("/api/orders/quote")) {
          const m = u.match(/con_id=(\d+)/);
          return {
            ok: true,
            json: async () => ({ token: `tok-${m?.[1]}` }),
          } as Response;
        }
        if (u === "/api/orders/place") {
          return {
            ok: true,
            json: async () => ({ orderId: "O1" }),
          } as Response;
        }
        return { ok: false, json: async () => ({}) } as Response;
      },
    );
  });

  it("disables submit until all tokens resolve, then POSTs with quote_tokens map", async () => {
    const prices = {
      SPY_20260516_500_C: { bid: 5.0, ask: 5.4 } as any,
      SPY_20260516_510_C: { bid: 1.8, ask: 2.2 } as any,
    };
    render(
      <PositionOrderModal
        position={comboPosition}
        prices={prices}
        onClose={() => {}}
      />,
    );

    const submit = (await screen.findByRole("button", {
      name: /submit close/i,
    })) as HTMLButtonElement;

    await waitFor(() => expect(submit.disabled).toBe(false));
    fireEvent.click(submit);
    await waitFor(() =>
      expect(
        ((global as any).fetch as ReturnType<typeof vi.fn>).mock.calls.some(
          (c: any[]) => c[0] === "/api/orders/place",
        ),
      ).toBe(true),
    );

    const placeCall = (
      (global as any).fetch as ReturnType<typeof vi.fn>
    ).mock.calls.find((c: any[]) => c[0] === "/api/orders/place");
    expect(placeCall).toBeDefined();
    const body = JSON.parse((placeCall![1] as RequestInit).body as string);
    expect(body.quote_tokens).toEqual({ "111": "tok-111", "222": "tok-222" });
    expect(body.quote_token).toBeUndefined();
  });
});
