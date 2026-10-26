/** @vitest-environment jsdom */
/**
 * X5b Task 3: the Futu orders view shows the order type label, trigger,
 * trail and session of each working order (contract § Working orders);
 * `limitPrice` null renders `---`. The IB view keeps its columns.
 * All symbols, prices and broker responses are synthetic.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, within } from "@testing-library/react";
import { OrderActionsProvider } from "@/lib/OrderActionsContext";
import { setFutuAccountEnv } from "@/lib/futuAccountEnv";
import { OrdersSections } from "@/components/workspace/OrdersSections";
import { fmtFutuPrice, futuOrderTermCells } from "@/lib/futuOrderTypes";
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

const base = {
  broker: "FUTU",
  accountEnv: "live",
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
  status: "Submitted",
  auxPrice: null,
  trailType: null,
  trailValue: null,
  trailSpread: null,
};
const stp = {
  ...base,
  brokerOrderId: "9001",
  orderType: "STP",
  limitPrice: null,
  auxPrice: 9.5,
  session: "RTH",
} as unknown as OpenOrder;
const trailLimit = {
  ...base,
  brokerOrderId: "9003",
  orderType: "TRAIL LIMIT",
  limitPrice: null,
  trailType: "AMOUNT",
  trailValue: 0.5,
  trailSpread: 0.005,
  session: "RTH",
} as unknown as OpenOrder;
const lmtEth = {
  ...base,
  brokerOrderId: "9004",
  orderType: "LMT",
  limitPrice: 0.1234,
  session: "ETH",
} as unknown as OpenOrder;

const snapshot = (rows: OpenOrder[]): OrdersData =>
  ({
    open_orders: rows,
    executed_orders: [],
    open_count: rows.length,
    executed_count: 0,
    last_sync: "2026-10-25T00:00:00Z",
  }) as unknown as OrdersData;

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
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            futu: {
              trading: { live: true, paper: true },
              capabilities: { live: { tuples: [], modify: ["LMT"] } },
            },
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
    ),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("futuOrderTermCells", () => {
  it("formats trigger, ratio / amount trail with spread, session; --- when absent", () => {
    expect(futuOrderTermCells(stp)).toEqual({
      trigger: "$9.50",
      trail: "---",
      session: "RTH",
    });
    expect(futuOrderTermCells(trailLimit)).toEqual({
      trigger: "---",
      trail: "$0.50 · spread $0.005",
      session: "RTH",
    });
    expect(
      futuOrderTermCells({ trailType: "RATIO", trailValue: 2.5 }).trail,
    ).toBe("2.5%");
    expect(futuOrderTermCells({}).session).toBe("---");
    expect(fmtFutuPrice(0.1234)).toBe("$0.1234");
    expect(fmtFutuPrice(null)).toBe("---");
  });
});

function rowFor(r: ReturnType<typeof render>, typeLabel: string) {
  const cell = r.getAllByRole("cell").find((c) => c.textContent === typeLabel);
  expect(cell).toBeTruthy();
  return cell!.closest("tr") as HTMLTableRowElement;
}

describe("Futu orders view columns", () => {
  it("adds Trigger / Trail / Session headers and per-type cells; null limit → ---", async () => {
    const r = render(
      <OrderActionsProvider>
        <OrdersSections
          orders={snapshot([stp, trailLimit, lmtEth])}
          activeAccount="futu"
          futuOrderActionsEnabled={true}
        />
      </OrderActionsProvider>,
    );
    await act(async () => {});
    for (const h of ["Trigger", "Trail", "Session"]) {
      expect(r.getByRole("columnheader", { name: h })).toBeTruthy();
    }
    const cells = (tr: HTMLTableRowElement) =>
      within(tr)
        .getAllByRole("cell")
        .map((c) => c.textContent);

    const stpRow = cells(rowFor(r, "STP"));
    // type, qty, limit, trigger, trail … session
    expect(stpRow.slice(2, 7)).toEqual(["STP", "10", "---", "$9.50", "---"]);
    expect(stpRow).toContain("RTH");

    const trailRow = cells(rowFor(r, "TRAIL LIMIT"));
    expect(trailRow.slice(2, 7)).toEqual([
      "TRAIL LIMIT",
      "10",
      "---",
      "---",
      "$0.50 · spread $0.005",
    ]);

    const lmtRow = cells(rowFor(r, "LMT"));
    expect(lmtRow.slice(2, 5)).toEqual(["LMT", "10", "$0.1234"]);
    expect(lmtRow).toContain("ETH");
  });

  it("IB view keeps its columns (no Trigger / Trail / Session)", async () => {
    const ib = {
      ...lmtEth,
      broker: "IB",
      brokerOrderId: undefined,
      accountEnv: undefined,
      orderId: 10,
      permId: 20,
      limitPrice: 10,
    } as unknown as OpenOrder;
    const r = render(
      <OrderActionsProvider>
        <OrdersSections orders={snapshot([ib])} activeAccount="ib" />
      </OrderActionsProvider>,
    );
    await act(async () => {});
    for (const h of ["Trigger", "Trail", "Session"]) {
      expect(r.queryByRole("columnheader", { name: h })).toBeNull();
    }
    expect(r.getByText("$10.00")).toBeTruthy();
  });
});

describe("Astra PR #247 — unknown trail unit display", () => {
  it.each([[null], [undefined], ["PERCENT"]])(
    "trailType %s renders --- for the trail value, never $",
    (trailType) => {
      const cells = futuOrderTermCells({ trailType, trailValue: 2 });
      expect(cells.trail).toBe("---");
      expect(
        futuOrderTermCells({ trailType, trailValue: 2, trailSpread: 0.05 })
          .trail,
      ).toBe("--- · spread $0.05");
    },
  );
});
