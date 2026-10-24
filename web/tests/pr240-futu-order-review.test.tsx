/** @vitest-environment jsdom */
/**
 * Regressions for Astra PR #240 review (X4b round 2). Ported from
 * /private/tmp/pr240-review-astra/replay.test.tsx and dialogs.test.tsx.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  renderHook,
  act,
  cleanup,
  render,
  fireEvent,
} from "@testing-library/react";
import {
  OrderActionsProvider,
  useOrderActions,
} from "@/lib/OrderActionsContext";
import { useOrders } from "@/lib/useOrders";
import { setFutuAccountEnv } from "@/lib/futuAccountEnv";
import { OrdersSections } from "@/components/workspace/OrdersSections";
import PositionTable from "@/components/PositionTable";
import {
  FUTU_MANAGEMENT_UNCONFIRMED_COPY,
  FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
} from "@/lib/futuOpenOrder";

const futuOrder = {
  broker: "FUTU",
  accountEnv: "live",
  brokerOrderId: "9001",
  orderId: 0,
  permId: 0,
  modifySequence: 1,
  symbol: "MOCK",
  contract: { symbol: "MOCK", secType: "STK" },
  action: "SELL",
  orderType: "LMT",
  totalQuantity: 1,
  remaining: 1,
  filled: 0,
  limitPrice: 10,
  tif: "DAY",
  status: "Submitted",
} as const;

const snapshot = (rows: unknown[]) => ({
  open_orders: rows,
  executed_orders: [],
  open_count: rows.length,
  executed_count: 0,
  last_sync: "2026-10-25T00:00:00Z",
});

const response = (body: unknown, ok = true, status = 200) => ({
  ok,
  status,
  json: async () => body,
});

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <OrderActionsProvider>{children}</OrderActionsProvider>
);

beforeEach(() => {
  vi.useFakeTimers();
  const storage = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => storage.get(k) ?? null,
      setItem: (k: string, v: string) => storage.set(k, v),
      removeItem: (k: string) => storage.delete(k),
      clear: () => storage.clear(),
    },
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("PR240 F5 uncertain management lock", () => {
  it("does not allow a second Futu cancel after FUTU_SEND_UNCERTAIN", async () => {
    const f = vi
      .fn()
      .mockResolvedValue(
        response({ reason_code: "FUTU_SEND_UNCERTAIN" }, false, 502),
      );
    vi.stubGlobal("fetch", f);
    const { result } = renderHook(() => useOrderActions(), { wrapper });
    await act(async () => {
      await result.current.requestCancel(futuOrder as never);
    });
    expect(result.current.isFutuManagementLocked(futuOrder as never)).toBe(
      true,
    );
    await act(async () => {
      await result.current.requestCancel(futuOrder as never);
    });
    expect(f).toHaveBeenCalledTimes(1);
  });
});

describe("PR240 F6/F7 Futu server-authoritative modify/cancel", () => {
  it("does not toast success when modify confirmed is false", async () => {
    const f = vi
      .fn()
      .mockResolvedValueOnce(
        response({ confirmed: false, applied_sequence: 2 }),
      );
    vi.stubGlobal("fetch", f);
    const { result } = renderHook(() => useOrderActions(), { wrapper });
    await act(async () => {
      await result.current.requestModify(futuOrder as never, { newPrice: 11 });
    });
    const notes = result.current.drainNotifications();
    expect(notes).toContainEqual(
      expect.objectContaining({
        type: "warning",
        message: FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
      }),
    );
    expect(notes.some((n) => n.type === "success")).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(f).toHaveBeenCalledTimes(1);
  });

  it("does not treat FILLED cancel as cancelled history", async () => {
    const f = vi.fn().mockResolvedValueOnce(
      response({
        confirmed: false,
        state: "FILLED",
        orders: snapshot([]),
      }),
    );
    vi.stubGlobal("fetch", f);
    const { result } = renderHook(() => useOrderActions(), { wrapper });
    await act(async () => {
      await result.current.requestCancel(futuOrder as never);
    });
    expect(result.current.cancelledOrders).toHaveLength(0);
    const notes = result.current.drainNotifications();
    expect(notes.some((n) => n.message.includes("filled"))).toBe(true);
    expect(notes.some((n) => n.message.includes("cancelled"))).toBe(false);
  });
});

describe("PR240 F8 modify sequence", () => {
  it.each(["FUTU", "IB"])(
    "advances modifySequence using applied_sequence for %s",
    async (broker) => {
      const f = vi
        .fn()
        .mockResolvedValueOnce(
          response({ confirmed: true, applied_sequence: 2 }),
        )
        .mockResolvedValueOnce(
          response({ confirmed: true, applied_sequence: 3 }),
        );
      vi.stubGlobal("fetch", f);
      const { result } = renderHook(() => useOrderActions(), { wrapper });
      const target =
        broker === "FUTU"
          ? futuOrder
          : {
              ...futuOrder,
              broker: "IB",
              brokerOrderId: undefined,
              orderId: 10,
              permId: 20,
            };
      await act(async () => {
        await result.current.requestModify(target as never, { newPrice: 11 });
      });
      await act(async () => {
        await result.current.requestModify(target as never, { newPrice: 12 });
      });
      const seqs = f.mock.calls.map(
        (c) => JSON.parse(String((c[1] as RequestInit).body)).modifySequence,
      );
      expect(seqs).toEqual([2, 3]);
    },
  );
});

describe("PR240 F3 scoped poll updates", () => {
  it("does not apply IB cancel poll data to a Futu paper updater", async () => {
    const ibOrder = {
      ...futuOrder,
      broker: "IB",
      brokerOrderId: undefined,
      orderId: 42,
      permId: 99,
      accountEnv: undefined,
    };
    const f = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/orders/cancel") {
        return Promise.resolve(response({ status: "ok" }));
      }
      if (url === "/api/orders" && init?.method === "POST") {
        return Promise.resolve(
          response(
            snapshot([
              {
                ...ibOrder,
                symbol: "LIVE-IB",
              },
            ]),
          ),
        );
      }
      return Promise.resolve(response(snapshot([])));
    });
    vi.stubGlobal("fetch", f);
    const { result } = renderHook(
      () => {
        const a = useOrderActions();
        const o = useOrders(false, "FUTU");
        React.useEffect(() => {
          a.setOrdersUpdater(o.updateData, "FUTU:paper");
          return () => a.setOrdersUpdater(null, null);
        }, [a.setOrdersUpdater, o.updateData]);
        return { a, o };
      },
      { wrapper },
    );
    await act(async () => {});
    await act(async () => {
      await result.current.a.requestCancel(ibOrder as never);
    });
    expect(result.current.o.data?.open_orders ?? []).toEqual([]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(result.current.o.data?.open_orders ?? []).toEqual([]);
  });
});

vi.mock("@/components/PositionOrderModal", () => ({
  default: ({ futuTicket }: { futuTicket?: unknown }) => (
    <div data-testid="ticket-route">{futuTicket ? "FUTU" : "IB"}</div>
  ),
}));
vi.mock("@/components/TickerLink", () => ({
  default: ({ ticker }: { ticker: string }) => <span>{ticker}</span>,
}));
vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));

describe("PR240 F1 Futu leg details", () => {
  it("does not POST unscoped IB place from expanded Futu leg UI", async () => {
    const f = vi.fn().mockResolvedValue(response({ status: "ok" }));
    vi.stubGlobal("fetch", f);
    const p = {
      id: 1,
      ticker: "MOCK",
      structure: "Long Call",
      structure_type: "Single",
      risk_profile: "defined",
      expiry: "2026-12-18",
      contracts: 1,
      direction: "LONG",
      entry_cost: 10,
      market_value: 10,
      legs: [
        {
          direction: "LONG",
          contracts: 1,
          type: "Call",
          strike: 10,
          avg_cost: 100,
          market_price: 1,
          market_value: 100,
          entry_cost: 100,
        },
        {
          direction: "SHORT",
          contracts: 1,
          type: "Call",
          strike: 11,
          avg_cost: 100,
          market_price: 1,
          market_value: 100,
          entry_cost: 100,
        },
      ],
    };
    const r = render(
      <PositionTable
        positions={[p]}
        prices={{}}
        portfolioPositions={[p]}
        readonly={false}
        futuOrderMode={true}
        futuAccountEnv="paper"
      />,
    );
    fireEvent.click(r.getByRole("button", { name: /Expand legs/i }));
    const leg = r.container.querySelector(".leg-clickable");
    expect(leg).not.toBeNull();
    fireEvent.click(leg!);
    expect(r.queryByRole("button", { name: "Place Order" })).toBeNull();
    expect(f).not.toHaveBeenCalled();
  });
});

describe("PR240 F2 ticket broker anchor", () => {
  it("closes Futu ticket when broker mode switches off", () => {
    const p = {
      id: 1,
      ticker: "MOCK",
      structure: "Stock",
      structure_type: "Stock",
      risk_profile: "equity",
      contracts: 1,
      direction: "LONG",
      entry_cost: 10,
      market_value: 10,
      legs: [
        {
          direction: "LONG",
          contracts: 1,
          type: "Stock",
          market_price: 10,
          market_value: 10,
          entry_cost: 10,
        },
      ],
    };
    const props = {
      positions: [p],
      prices: {},
      portfolioPositions: [p],
      readonly: false,
      futuAccountEnv: "paper" as const,
    };
    const r = render(<PositionTable {...props} futuOrderMode={true} />);
    fireEvent.click(r.getByRole("button", { name: /order/i }));
    expect(r.getByTestId("ticket-route").textContent).toBe("FUTU");
    r.rerender(<PositionTable {...props} futuOrderMode={false} />);
    expect(r.queryByTestId("ticket-route")).toBeNull();
  });
});

vi.mock("@/components/workspace/HistoricalTradesSection", () => ({
  HistoricalTradesSection: () => null,
}));
vi.mock("@/components/CancelOrderDialog", () => ({
  default: ({ order, onConfirm }: { order: unknown; onConfirm: () => void }) =>
    order ? (
      <button type="button" onClick={onConfirm}>
        Confirm retained cancel
      </button>
    ) : null,
}));
vi.mock("@/components/ModifyOrderModal", () => ({
  default: () => null,
}));

describe("PR240 F4 dialog scope", () => {
  it("opens a live cancel dialog with trading enabled, then drops it on env switch without sending", async () => {
    vi.useRealTimers();
    // Health must enable trading, else the dialog never opens and the test is vacuous.
    const f = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ futu: { trading: { live: true, paper: true } } }),
    });
    vi.stubGlobal("fetch", f);
    setFutuAccountEnv("live");
    const order = { ...futuOrder };
    const snap = snapshot([order]);
    const r = render(
      <OrderActionsProvider>
        <OrdersSections
          orders={snap as never}
          activeAccount="futu"
          futuOrderActionsEnabled={true}
        />
      </OrderActionsProvider>,
    );
    await act(async () => {});
    fireEvent.click(r.getByRole("button", { name: "CANCEL" }));
    expect(
      r.getByRole("button", { name: "Confirm retained cancel" }),
    ).toBeTruthy();
    await act(async () => {
      setFutuAccountEnv("paper");
    });
    expect(
      r.queryByRole("button", { name: "Confirm retained cancel" }),
    ).toBeNull();
    expect(f.mock.calls.every((c) => c[0] === "/api/health")).toBe(true);
  });
});
