/** @vitest-environment jsdom */
/**
 * Regressions for Astra PR #240 round-2 findings (R2-1 .. R2-5). Ported from
 * /private/tmp/pr240-review-astra/round2-hook.test.tsx and round2-ui.test.tsx;
 * each replay's defect assertion is inverted to the required behavior.
 * All symbols, prices and broker responses are synthetic; fetch is mocked.
 */
import React, { useEffect } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  renderHook,
  render,
  fireEvent,
  act,
  cleanup,
} from "@testing-library/react";
import {
  OrderActionsProvider,
  useOrderActions,
} from "@/lib/OrderActionsContext";
import { useOrders } from "@/lib/useOrders";
import { setFutuAccountEnv } from "@/lib/futuAccountEnv";
import { OrdersSections } from "@/components/workspace/OrdersSections";
import WorkspaceSections from "@/components/workspace/WorkspaceSections";
import {
  TickerDetailProvider,
  useTickerDetail,
} from "@/lib/TickerDetailContext";
import {
  FUTU_MANAGEMENT_UNCONFIRMED_COPY,
  FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
} from "@/lib/futuOpenOrder";
import type { OpenOrder, OrdersData, PortfolioData } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/MOCK",
}));
vi.mock("@/lib/useWatchlist", () => ({
  useWatchlist: () => ({ isWatched: () => false, toggleWatch: vi.fn() }),
}));
vi.mock("@/components/TickerLink", () => ({
  default: ({ ticker }: { ticker: string }) => <span>{ticker}</span>,
}));
vi.mock("@/components/workspace/HistoricalTradesSection", () => ({
  HistoricalTradesSection: () => null,
}));
vi.mock("@/components/CancelOrderDialog", () => ({
  default: ({ order, onConfirm }: { order: unknown; onConfirm: () => void }) =>
    order ? <button onClick={onConfirm}>Confirm retained cancel</button> : null,
}));
vi.mock("@/components/ModifyOrderModal", () => ({
  default: ({
    order,
    onConfirm,
  }: {
    order: unknown;
    onConfirm: (r: { newPrice: number }) => void;
  }) =>
    order ? (
      <button onClick={() => onConfirm({ newPrice: 11 })}>
        Confirm retained modify
      </button>
    ) : null,
}));

const order = {
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
} as unknown as OpenOrder;
const ib = {
  ...order,
  broker: "IB",
  brokerOrderId: undefined,
  accountEnv: undefined,
  orderId: 10,
  permId: 20,
} as unknown as OpenOrder;

const snapshot = (rows: OpenOrder[]): OrdersData =>
  ({
    open_orders: rows,
    executed_orders: [],
    open_count: rows.length,
    executed_count: 0,
    last_sync: "2026-10-25T00:00:00Z",
  }) as unknown as OrdersData;
const response = (body: unknown, ok = true, status = 200) => ({
  ok,
  status,
  json: async () => body,
});
// Contract § Order types: clients offer modify only for capabilities[env].modify.
const health = {
  futu: {
    trading: { live: true, paper: true },
    capabilities: {
      live: { tuples: ["STK:LMT:DAY:RTH"], modify: ["LMT"] },
      paper: { tuples: ["STK:LMT:DAY:RTH"], modify: ["LMT"] },
    },
  },
};
const LIVE_VIEW = "/api/orders?broker=FUTU&account_env=live";

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <OrderActionsProvider>{children}</OrderActionsProvider>
);

/** Futu live view hook wired the way WorkspaceShell wires it. */
function useFutuLiveView() {
  const a = useOrderActions();
  const o = useOrders(false, "FUTU");
  const { setOrdersUpdater } = a;
  const { updateData } = o;
  useEffect(() => {
    setOrdersUpdater(updateData, "FUTU:live");
    return () => setOrdersUpdater(null, null);
  }, [setOrdersUpdater, updateData]);
  return { a, o };
}

function postsTo(f: ReturnType<typeof vi.fn>, url: string): number {
  return f.mock.calls.filter(
    (c) => c[0] === url && (c[1] as RequestInit | undefined)?.method === "POST",
  ).length;
}

beforeEach(() => {
  const s = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => s.get(k) ?? null,
      setItem: (k: string, v: string) => s.set(k, v),
      removeItem: (k: string) => s.delete(k),
      clear: () => s.clear(),
    },
  });
  setFutuAccountEnv("live");
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("R2-2 confirmed:false Futu modify is not a lock", () => {
  it("does not mark pending, warns, and refetches the current scoped view", async () => {
    vi.useFakeTimers();
    let rows = [order];
    const f = vi.fn((url: string) =>
      Promise.resolve(
        response(
          url.includes("/modify")
            ? { confirmed: false, applied_sequence: 2, orders: snapshot(rows) }
            : snapshot(rows),
        ),
      ),
    );
    vi.stubGlobal("fetch", f);
    const { result } = renderHook(useFutuLiveView, { wrapper });
    await act(async () => {});
    await act(async () => {
      await result.current.a.requestModify(order, { newPrice: 11 });
    });
    expect(result.current.a.isPendingModify(order)).toBe(false);
    expect(postsTo(f, LIVE_VIEW)).toBe(1);
    const notes = result.current.a.drainNotifications();
    expect(notes).toContainEqual(
      expect.objectContaining({
        type: "warning",
        message: FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
      }),
    );
    expect(notes.some((n) => n.type === "success")).toBe(false);
    rows = [{ ...order, limitPrice: 11, modifySequence: 2 }];
    await act(async () => {
      result.current.o.syncNow();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(180_000);
    });
    expect(result.current.o.data?.open_orders[0].limitPrice).toBe(11);
    expect(result.current.a.isPendingModify(rows[0])).toBe(false);
    expect(
      result.current.a.drainNotifications().some((n) => n.type === "success"),
    ).toBe(false);
  });

  it("keeps MODIFY and CANCEL on the row after a confirmed:false modify", async () => {
    const f = vi.fn((url: string) =>
      Promise.resolve(
        url === "/api/health"
          ? response(health)
          : url.includes("/modify")
            ? response({ confirmed: false, applied_sequence: 2 })
            : response(snapshot([order])),
      ),
    );
    vi.stubGlobal("fetch", f);
    const r = render(
      <OrderActionsProvider>
        <OrdersSections
          orders={snapshot([order])}
          activeAccount="futu"
          futuOrderActionsEnabled={true}
        />
      </OrderActionsProvider>,
    );
    await act(async () => {});
    fireEvent.click(r.getByRole("button", { name: "MODIFY" }));
    await act(async () => {
      fireEvent.click(
        r.getByRole("button", { name: "Confirm retained modify" }),
      );
    });
    expect(r.queryByText("PENDING")).toBeNull();
    expect(r.getByRole("button", { name: "MODIFY" })).toBeTruthy();
    expect(r.getByRole("button", { name: "CANCEL" })).toBeTruthy();
  });
});

describe("Futu cancel refetches the current scoped view", () => {
  it("POSTs only the current view after a confirmed cancel", async () => {
    const f = vi.fn((url: string) =>
      Promise.resolve(
        url.includes("/cancel")
          ? response({
              confirmed: true,
              state: "CANCELLED",
              brokerOrderId: "9001",
              orders: snapshot([]),
            })
          : response(snapshot([])),
      ),
    );
    vi.stubGlobal("fetch", f);
    const { result } = renderHook(useFutuLiveView, { wrapper });
    await act(async () => {});
    await act(async () => {
      await result.current.a.requestCancel(order);
    });
    expect(postsTo(f, LIVE_VIEW)).toBe(1);
    expect(postsTo(f, "/api/orders")).toBe(0);
    expect(postsTo(f, "/api/orders?broker=FUTU&account_env=paper")).toBe(0);
  });
});

describe("R2-4 uncertain Futu management lock", () => {
  it("stays locked on unchanged terms and clears when a same-scope refresh shows new terms", async () => {
    let rows = [order];
    const f = vi.fn((url: string) =>
      Promise.resolve(
        url.includes("/modify")
          ? response({ reason_code: "FUTU_SEND_UNCERTAIN" }, false, 502)
          : response(snapshot(rows)),
      ),
    );
    vi.stubGlobal("fetch", f);
    const { result } = renderHook(useFutuLiveView, { wrapper });
    await act(async () => {});
    await act(async () => {
      await result.current.a.requestModify(order, { newPrice: 11 });
    });
    expect(result.current.a.isFutuManagementLocked(order)).toBe(true);
    // A same-terms refresh does not clear the lock, and resend is refused.
    await act(async () => {
      result.current.o.syncNow();
    });
    expect(result.current.a.isFutuManagementLocked(order)).toBe(true);
    const sends = f.mock.calls.filter((c) => c[0].includes("/modify")).length;
    await act(async () => {
      await result.current.a.requestModify(order, { newPrice: 11 });
    });
    expect(f.mock.calls.filter((c) => c[0].includes("/modify")).length).toBe(
      sends,
    );
    rows = [{ ...order, limitPrice: 11, modifySequence: 2 }];
    await act(async () => {
      result.current.o.syncNow();
    });
    expect(result.current.o.data?.open_orders[0].limitPrice).toBe(11);
    expect(result.current.a.isFutuManagementLocked(rows[0])).toBe(false);
  });

  it("clears when a same-scope refresh shows the order gone", async () => {
    let rows = [order];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          url.includes("/cancel")
            ? response({ detail: "upstream" }, false, 500)
            : response(snapshot(rows)),
        ),
      ),
    );
    const { result } = renderHook(useFutuLiveView, { wrapper });
    await act(async () => {});
    await act(async () => {
      await result.current.a.requestCancel(order);
    });
    expect(result.current.a.isFutuManagementLocked(order)).toBe(true);
    rows = [];
    await act(async () => {
      result.current.o.syncNow();
    });
    expect(result.current.a.isFutuManagementLocked(order)).toBe(false);
  });

  it("renders the unconfirmed copy and no CANCEL/MODIFY buttons", async () => {
    const f = vi.fn((url: string) =>
      Promise.resolve(
        url === "/api/health"
          ? response(health)
          : url.includes("/cancel")
            ? response({ reason_code: "FUTU_SEND_UNCERTAIN" }, false, 502)
            : response(snapshot([order])),
      ),
    );
    vi.stubGlobal("fetch", f);
    const tree = (rows: OpenOrder[]) => (
      <OrderActionsProvider>
        <OrdersSections
          orders={snapshot(rows)}
          activeAccount="futu"
          futuOrderActionsEnabled={true}
        />
      </OrderActionsProvider>
    );
    const r = render(tree([order]));
    await act(async () => {});
    fireEvent.click(r.getByRole("button", { name: "CANCEL" }));
    await act(async () => {
      fireEvent.click(
        r.getByRole("button", { name: "Confirm retained cancel" }),
      );
    });
    expect(
      r.queryByRole("button", { name: "Confirm retained cancel" }),
    ).toBeNull();
    expect(r.getByText(FUTU_MANAGEMENT_UNCONFIRMED_COPY)).toBeTruthy();
    expect(r.queryByRole("button", { name: "CANCEL" })).toBeNull();
    expect(r.queryByRole("button", { name: "MODIFY" })).toBeNull();
    // A refreshed row with changed terms restores the controls.
    r.rerender(tree([{ ...order, limitPrice: 12, modifySequence: 2 }]));
    expect(r.queryByText(FUTU_MANAGEMENT_UNCONFIRMED_COPY)).toBeNull();
    expect(r.getByRole("button", { name: "CANCEL" })).toBeTruthy();
  });
});

describe("R2-3 replacement refresh honours the captured scope", () => {
  it("does not publish an IB snapshot into a newly selected Futu view", async () => {
    let resolve!: (v: unknown) => void;
    const deferred = new Promise((r) => (resolve = r));
    const f = vi.fn((url: string) =>
      url.includes("/modify")
        ? deferred
        : Promise.resolve(response(snapshot([ib]))),
    );
    vi.stubGlobal("fetch", f);
    const { result } = renderHook(useOrderActions, { wrapper });
    const old = vi.fn();
    const current = vi.fn();
    result.current.setOrdersUpdater(old, "IB");
    let pending!: Promise<unknown>;
    act(() => {
      pending = result.current.requestModify(ib, {
        newPrice: 11,
        replaceOrder: { legs: [] },
      } as never);
    });
    result.current.setOrdersUpdater(current, "FUTU:paper");
    await act(async () => {
      resolve(response({ status: "ok" }));
      await pending;
      await Promise.resolve();
    });
    expect(old).not.toHaveBeenCalled();
    expect(current).not.toHaveBeenCalled();
  });

  it("publishes the raw snapshot when the IB view is still selected", async () => {
    const f = vi.fn((url: string) =>
      Promise.resolve(
        url.includes("/modify")
          ? response({ status: "ok" })
          : response(snapshot([ib])),
      ),
    );
    vi.stubGlobal("fetch", f);
    const { result } = renderHook(useOrderActions, { wrapper });
    const update = vi.fn();
    result.current.setOrdersUpdater(update, "IB");
    await act(async () => {
      await result.current.requestModify(ib, {
        newPrice: 11,
        replaceOrder: { legs: [] },
      } as never);
      await Promise.resolve();
    });
    expect(update).toHaveBeenCalledWith(snapshot([ib]));
  });
});

describe("R2-5 IB modify timeout publishes the broker's actual price", () => {
  it("last published row shows the actual price after timeout", async () => {
    vi.useFakeTimers();
    const f = vi
      .fn()
      .mockResolvedValueOnce(
        response({
          confirmed: true,
          orders: snapshot([{ ...ib, limitPrice: 11 }]),
        }),
      )
      .mockResolvedValue(response(snapshot([ib])));
    vi.stubGlobal("fetch", f);
    const { result } = renderHook(useOrderActions, { wrapper });
    const update = vi.fn();
    result.current.setOrdersUpdater(update, "IB");
    await act(async () => {
      await result.current.requestModify(ib, { newPrice: 11 });
    });
    for (let i = 0; i < 24; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
    }
    expect(result.current.isPendingModify(ib)).toBe(false);
    expect(result.current.drainNotifications()).toContainEqual(
      expect.objectContaining({ message: "MOCK modify not confirmed by IB" }),
    );
    expect(update.mock.lastCall?.[0].open_orders[0].limitPrice).toBe(10);
  });
});

describe("R2-1 ticker details expose no IB execution for Futu", () => {
  const position = {
    id: 1,
    broker: "FUTU",
    ticker: "MOCK",
    structure: "Stock",
    structure_type: "Stock",
    risk_profile: "equity",
    expiry: "N/A",
    contracts: 1,
    direction: "LONG",
    entry_cost: 10,
    market_value: 10,
    legs: [
      {
        direction: "LONG",
        contracts: 1,
        type: "Stock",
        strike: null,
        market_price: 10,
        market_value: 10,
        entry_cost: 10,
      },
    ],
  };
  const portfolio = { positions: [position] } as unknown as PortfolioData;

  function Seed({ children }: { children: React.ReactNode }) {
    const { setPortfolio } = useTickerDetail();
    setPortfolio(portfolio);
    return <>{children}</>;
  }

  const tree = (activeAccount: "ib" | "futu") => (
    <TickerDetailProvider>
      <OrderActionsProvider>
        <Seed>
          <WorkspaceSections
            section="ticker-detail"
            tickerParam="MOCK"
            activeAccount={activeAccount}
            futuOrderActionsEnabled={activeAccount === "futu"}
          />
        </Seed>
      </OrderActionsProvider>
    </TickerDetailProvider>
  );

  it("Futu account: no Place Order surface and no IB place request", async () => {
    const f = vi.fn().mockResolvedValue(response({ status: "ok" }));
    vi.stubGlobal("fetch", f);
    const r = render(tree("futu"));
    await act(async () => {});
    expect(r.container.querySelector(".cockpit")).toBeTruthy();
    expect(r.queryByRole("button", { name: "Place Order" })).toBeNull();
    expect(r.container.querySelector(".modify-price-input")).toBeNull();
    expect(
      f.mock.calls.some((c) => String(c[0]).startsWith("/api/orders/place")),
    ).toBe(false);
  });

  it("IB account: the same route still reaches the real IB submit path", async () => {
    const f = vi
      .fn()
      .mockResolvedValue(response({ status: "ok", orderId: "mock" }));
    vi.stubGlobal("fetch", f);
    const r = render(tree("ib"));
    await act(async () => {});
    const input = r.container.querySelector(".modify-price-input");
    expect(input).not.toBeNull();
    fireEvent.change(input!, { target: { value: "10" } });
    fireEvent.click(r.getByRole("button", { name: "Place Order" }));
    await act(async () => {
      fireEvent.click(r.getByRole("button", { name: "Confirm Order" }));
    });
    expect(f.mock.calls.some((c) => c[0] === "/api/orders/place")).toBe(true);
  });
});
