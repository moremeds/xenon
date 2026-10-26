/** @vitest-environment jsdom */
/**
 * X5b Task 2 (Astra A6): route → context regressions for Futu modify of the
 * trigger / trail terms. The browser fetch goes through the real
 * /api/orders/modify route; only the FastAPI call (xenonFetch) is mocked.
 * Covers aux-only and trail-only changes, unconfirmed and uncertain replies,
 * no duplicate retry, refresh with changed terms, modify gating by
 * capabilities.modify, the Futu modify dialog fields, and IB unchanged.
 * All symbols, prices and broker responses are synthetic.
 */
import React, { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
} from "@testing-library/react";
import {
  OrderActionsProvider,
  useOrderActions,
} from "@/lib/OrderActionsContext";
import { useOrders } from "@/lib/useOrders";
import { setFutuAccountEnv } from "@/lib/futuAccountEnv";
import { OrdersSections } from "@/components/workspace/OrdersSections";
import ModifyOrderModal from "@/components/ModifyOrderModal";
import {
  FUTU_MANAGEMENT_UNCONFIRMED_COPY,
  FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
} from "@/lib/futuOpenOrder";
import { XenonApiError } from "@/lib/xenonApi";
import type { OpenOrder, OrdersData } from "@/lib/types";

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
vi.mock("@/components/Modal", () => ({
  default: ({
    children,
    open,
  }: {
    children: React.ReactNode;
    open: boolean;
  }) => (open ? <div data-testid="modal">{children}</div> : null),
}));

const xenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/xenonApi")>("@/lib/xenonApi");
  return { ...actual, xenonFetch: (...args: unknown[]) => xenonFetch(...args) };
});

const base = {
  broker: "FUTU",
  accountEnv: "live",
  brokerOrderId: "9001",
  orderId: 0,
  permId: 0,
  modifySequence: 1,
  symbol: "MOCK",
  contract: { symbol: "MOCK", secType: "STK" },
  action: "SELL",
  totalQuantity: 10,
  remaining: 10,
  filled: 0,
  tif: "GTC",
  session: "RTH",
  status: "Submitted",
  trailType: null,
  trailValue: null,
  trailSpread: null,
};
const stp = {
  ...base,
  orderType: "STP",
  limitPrice: null,
  auxPrice: 9.5,
} as unknown as OpenOrder;
const trail = {
  ...base,
  brokerOrderId: "9002",
  orderType: "TRAIL",
  limitPrice: null,
  auxPrice: null,
  trailType: "RATIO",
  trailValue: 2,
} as unknown as OpenOrder;
const trailLimit = {
  ...base,
  brokerOrderId: "9003",
  orderType: "TRAIL LIMIT",
  limitPrice: null,
  auxPrice: null,
  trailType: "AMOUNT",
  trailValue: 0.5,
  trailSpread: 0.05,
} as unknown as OpenOrder;
const ibLmt = {
  ...base,
  broker: "IB",
  accountEnv: undefined,
  brokerOrderId: undefined,
  orderId: 10,
  permId: 20,
  orderType: "LMT",
  limitPrice: 10,
  auxPrice: null,
  tif: "DAY",
} as unknown as OpenOrder;

const snapshot = (rows: OpenOrder[]): OrdersData =>
  ({
    open_orders: rows,
    executed_orders: [],
    open_count: rows.length,
    executed_count: 0,
    last_sync: "2026-10-25T00:00:00Z",
  }) as unknown as OrdersData;

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

let rows: OpenOrder[] = [];
let modifyModes: string[] = [];
let modifyTypes = ["LMT"];

/** Browser fetch: /api/orders/modify runs the real route; the rest is mocked. */
function installFetch() {
  const f = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/health")
      return jsonResponse({
        futu: {
          trading: { live: true, paper: true },
          capabilities: {
            live: { tuples: [], modify: modifyTypes },
            paper: { tuples: [], modify: ["LMT"] },
          },
        },
      });
    if (url.startsWith("/api/orders/modify")) {
      const { POST } = await import("../app/api/orders/modify/route");
      return POST(new Request(`http://localhost${url}`, init));
    }
    if (url.startsWith("/api/orders/cancel"))
      return jsonResponse({
        status: "ok",
        brokerOrderId: "9001",
        state: "CANCELLED",
        confirmed: true,
      });
    return jsonResponse(snapshot(rows));
  });
  vi.stubGlobal("fetch", f);
  return f;
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
  rows = [stp];
  modifyModes = [];
  modifyTypes = ["LMT"];
  xenonFetch.mockReset();
  xenonFetch.mockImplementation(async (path: string) => {
    if (String(path).startsWith("/orders/modify")) {
      const mode = modifyModes.shift() ?? "unconfirmed";
      if (mode === "uncertain")
        throw new XenonApiError(502, "send uncertain", {
          reason_code: "FUTU_SEND_UNCERTAIN",
        });
      return {
        status: "ok",
        confirmed: mode === "confirmed",
        applied_sequence: 2,
      };
    }
    if (String(path) === "/orders" || String(path).startsWith("/orders?"))
      return snapshot([...rows, ibLmt]);
    return {};
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <OrderActionsProvider>{children}</OrderActionsProvider>
);

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

const upstreamModifies = () =>
  xenonFetch.mock.calls
    .filter(([p]) => String(p).startsWith("/orders/modify"))
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));

describe("route → context: aux-only / trail-only Futu modify", () => {
  it("aux-only confirmed:false: newAuxPrice reaches FastAPI, warning, never success", async () => {
    installFetch();
    const { result } = renderHook(useFutuLiveView, { wrapper });
    await act(async () => {});
    let r: Awaited<ReturnType<typeof result.current.a.requestModify>>;
    await act(async () => {
      r = await result.current.a.requestModify(stp, { newAuxPrice: 9.25 });
    });
    expect(r!.ok).toBe(true);
    expect(upstreamModifies()).toEqual([
      { brokerOrderId: "9001", newAuxPrice: 9.25, modifySequence: 2 },
    ]);
    const notes = result.current.a.drainNotifications();
    expect(notes).toContainEqual(
      expect.objectContaining({
        type: "warning",
        message: FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
      }),
    );
    expect(notes.some((n) => n.type === "success")).toBe(false);
    expect(result.current.a.isPendingModify(stp)).toBe(false);
  });

  it("trail-only confirmed:true: success names the new trail", async () => {
    rows = [trail];
    modifyModes = ["confirmed"];
    installFetch();
    const { result } = renderHook(useFutuLiveView, { wrapper });
    await act(async () => {});
    await act(async () => {
      await result.current.a.requestModify(trail, { newTrailValue: 2.5 });
    });
    expect(upstreamModifies()).toEqual([
      { brokerOrderId: "9002", newTrailValue: 2.5, modifySequence: 2 },
    ]);
    expect(result.current.a.drainNotifications()).toContainEqual({
      type: "success",
      message: "MOCK order confirmed (trail 2.5%)",
    });
  });

  it("uncertain aux-only: locks, no duplicate send, same terms keep the lock, a changed trigger releases it", async () => {
    modifyModes = ["uncertain"];
    installFetch();
    const { result } = renderHook(useFutuLiveView, { wrapper });
    await act(async () => {});
    let r: Awaited<ReturnType<typeof result.current.a.requestModify>>;
    await act(async () => {
      r = await result.current.a.requestModify(stp, { newAuxPrice: 9.25 });
    });
    expect(r!.ok).toBe(false);
    expect(result.current.a.isFutuManagementLocked(stp)).toBe(true);
    expect(result.current.a.drainNotifications()).toContainEqual(
      expect.objectContaining({ message: FUTU_MANAGEMENT_UNCONFIRMED_COPY }),
    );

    // No duplicate retry while locked.
    await act(async () => {
      await result.current.a.requestModify(stp, { newAuxPrice: 9.25 });
    });
    expect(upstreamModifies()).toHaveLength(1);

    // Same terms on refresh: still locked.
    await act(async () => {
      result.current.o.syncNow();
    });
    expect(result.current.a.isFutuManagementLocked(stp)).toBe(true);

    // Only the trigger changed (same sequence, price, qty): released.
    rows = [{ ...stp, auxPrice: 9.25 }];
    await act(async () => {
      result.current.o.syncNow();
    });
    expect(result.current.o.data?.open_orders[0].auxPrice).toBe(9.25);
    expect(result.current.a.isFutuManagementLocked(rows[0])).toBe(false);
  });

  it("uncertain trail-only: a refreshed row with a changed trail value releases the lock", async () => {
    rows = [trailLimit];
    modifyModes = ["uncertain"];
    installFetch();
    const { result } = renderHook(useFutuLiveView, { wrapper });
    await act(async () => {});
    await act(async () => {
      await result.current.a.requestModify(trailLimit, {
        newTrailSpread: 0.04,
      });
    });
    expect(result.current.a.isFutuManagementLocked(trailLimit)).toBe(true);
    rows = [{ ...trailLimit, trailSpread: 0.04 }];
    await act(async () => {
      result.current.o.syncNow();
    });
    expect(result.current.a.isFutuManagementLocked(rows[0])).toBe(false);
  });

  it("IB modify never carries the Futu terms", async () => {
    const f = installFetch();
    const { result } = renderHook(() => useOrderActions(), { wrapper });
    await act(async () => {
      await result.current.requestModify(ibLmt, {
        newPrice: 11,
        newAuxPrice: 9,
      });
    });
    const call = f.mock.calls.find(([u]) => u === "/api/orders/modify");
    const body = JSON.parse(String((call![1] as RequestInit).body));
    expect(body).toEqual({
      orderId: 10,
      permId: 20,
      newPrice: 11,
      modifySequence: 2,
    });
  });
});

describe("Orders view: modify per capabilities.modify, cancel for every type", () => {
  const tree = (orderRows: OpenOrder[]) => (
    <OrderActionsProvider>
      <OrdersSections
        orders={snapshot(orderRows)}
        activeAccount="futu"
        futuOrderActionsEnabled={true}
      />
    </OrderActionsProvider>
  );

  it("STP with modify=[LMT]: MODIFY disabled, CANCEL enabled", async () => {
    installFetch();
    const r = render(tree([stp]));
    await act(async () => {});
    const modifyBtn = r.getByRole("button", { name: "MODIFY" });
    expect((modifyBtn as HTMLButtonElement).disabled).toBe(true);
    expect(modifyBtn.getAttribute("title")).toMatch(/cancel instead/);
    expect(
      (r.getByRole("button", { name: "CANCEL" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("STP with modify=[LMT, STP]: dialog shows the trigger only; aux-only send", async () => {
    modifyTypes = ["LMT", "STP"];
    installFetch();
    const r = render(tree([stp]));
    await act(async () => {});
    fireEvent.click(r.getByRole("button", { name: "MODIFY" }));
    expect(r.getByLabelText(/^New Trigger Price/)).toBeTruthy();
    expect(r.queryByLabelText(/^New Limit Price/)).toBeNull();
    expect(r.queryByText("FILL OUTSIDE RTH")).toBeNull();
    const submit = r.getByRole("button", { name: "Modify Order" });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(r.getByLabelText(/^New Trigger Price/), {
      target: { value: "9.25" },
    });
    expect(r.getByTestId("futu-modify-confirm").textContent).toBe(
      "trigger 9.50 → 9.25",
    );
    await act(async () => {
      fireEvent.click(r.getByRole("button", { name: "Modify Order" }));
    });
    expect(upstreamModifies()).toEqual([
      { brokerOrderId: "9001", newAuxPrice: 9.25, modifySequence: 2 },
    ]);
  });
});

describe("ModifyOrderModal fields per Futu type; IB unchanged", () => {
  it("TRAIL LIMIT: trail value + spread, no limit / trigger; spread-only request", () => {
    const onConfirm = vi.fn();
    const r = render(
      <ModifyOrderModal
        order={trailLimit}
        loading={false}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );
    expect(r.getByLabelText(/^New Trail Value \(AMOUNT\)/)).toBeTruthy();
    expect(r.getByLabelText(/^New Trail Spread/)).toBeTruthy();
    expect(r.queryByLabelText(/^New Limit Price/)).toBeNull();
    expect(r.queryByLabelText(/^New Trigger Price/)).toBeNull();
    fireEvent.change(r.getByLabelText(/^New Trail Spread/), {
      target: { value: "0" },
    });
    fireEvent.click(r.getByRole("button", { name: "Modify Order" }));
    expect(onConfirm).toHaveBeenCalledWith({ newTrailSpread: 0 });
  });

  it("switching between two Futu orders reseeds the fields (permId is 0 on both)", () => {
    const props = { loading: false, onConfirm: vi.fn(), onClose: () => {} };
    const r = render(<ModifyOrderModal order={stp} {...props} />);
    expect(
      (r.getByLabelText(/^New Trigger Price/) as HTMLInputElement).value,
    ).toBe("9.5");
    r.rerender(
      <ModifyOrderModal
        order={{ ...stp, brokerOrderId: "9009", auxPrice: 8 } as OpenOrder}
        {...props}
      />,
    );
    expect(
      (r.getByLabelText(/^New Trigger Price/) as HTMLInputElement).value,
    ).toBe("8");
  });

  it("IB LMT: limit + RTH checkbox, no trigger / trail", () => {
    const onConfirm = vi.fn();
    const r = render(
      <ModifyOrderModal
        order={ibLmt}
        loading={false}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );
    expect(r.getByLabelText(/^New Limit Price/)).toBeTruthy();
    expect(r.getByText("FILL OUTSIDE RTH")).toBeTruthy();
    expect(r.queryByLabelText(/^New Trigger Price/)).toBeNull();
    fireEvent.change(r.getByLabelText(/^New Limit Price/), {
      target: { value: "11" },
    });
    fireEvent.click(r.getByRole("button", { name: "Modify Order" }));
    expect(onConfirm).toHaveBeenCalledWith({ newPrice: 11 });
  });
});

describe("Astra PR #247 — modify dialog precision and trail unit", () => {
  const trailNoUnit = {
    ...trail,
    brokerOrderId: "9005",
    trailType: null,
    trailValue: 2,
  } as unknown as OpenOrder;

  it("sub-dollar BID reference keeps 0.5123 for a Futu trigger", () => {
    const onConfirm = vi.fn();
    const r = render(
      <ModifyOrderModal
        order={{ ...stp, auxPrice: 0.6 } as OpenOrder}
        prices={{
          MOCK: { last: 0.5124, bid: 0.5123, ask: 0.5125 } as never,
        }}
        loading={false}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );
    const bid = r.getByRole("button", { name: /^BID/ });
    expect(bid.textContent).toBe("BID 0.5123");
    fireEvent.click(bid);
    expect(
      (r.getByLabelText(/^New Trigger Price/) as HTMLInputElement).value,
    ).toBe("0.5123");
    fireEvent.click(r.getByRole("button", { name: "Modify Order" }));
    expect(onConfirm).toHaveBeenCalledWith({ newAuxPrice: 0.5123 });
  });

  it("unknown trail unit: --- marker, trail value not editable, never sent", () => {
    const onConfirm = vi.fn();
    const r = render(
      <ModifyOrderModal
        order={trailNoUnit}
        loading={false}
        onConfirm={onConfirm}
        onClose={() => {}}
      />,
    );
    const input = r.getByLabelText(/^New Trail Value/) as HTMLInputElement;
    expect(input.disabled).toBe(true);
    const row = input.closest(".modify-price-input-row")!;
    expect(row.querySelector(".modify-price-prefix")?.textContent).toBe("---");
    expect(row.textContent).not.toContain("$");
    fireEvent.change(input, { target: { value: "3" } });
    fireEvent.click(r.getByRole("button", { name: "Modify Order" }));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("unknown trail unit row: Trail shows ---, CANCEL stays available", async () => {
    modifyTypes = ["LMT", "TRAIL"];
    rows = [trailNoUnit];
    const f = installFetch();
    const r = render(
      <OrderActionsProvider>
        <OrdersSections
          orders={snapshot([trailNoUnit])}
          activeAccount="futu"
          futuOrderActionsEnabled={true}
        />
      </OrderActionsProvider>,
    );
    await act(async () => {});
    expect(r.getByTestId("futu-order-trail").textContent).toBe("---");
    const cancel = r.getByRole("button", { name: "CANCEL" });
    expect((cancel as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(cancel);
    await act(async () => {
      fireEvent.click(r.getByRole("button", { name: "Cancel Order" }));
    });
    const call = f.mock.calls.find(([u]) =>
      String(u).startsWith("/api/orders/cancel"),
    );
    expect(JSON.parse(String((call![1] as RequestInit).body))).toEqual({
      brokerOrderId: "9005",
    });
  });
});
