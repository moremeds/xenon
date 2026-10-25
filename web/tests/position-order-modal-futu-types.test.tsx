/**
 * @vitest-environment jsdom
 *
 * X5b Task 1: the Futu ticket offers order type / TIF / session only from
 * /api/health futu.capabilities[env].tuples for the row's security type,
 * shows the fields of the chosen type, and never sends an absent field.
 * The IB ticket is unchanged.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  waitFor,
  within,
} from "@testing-library/react";
import PositionOrderModal from "@/components/PositionOrderModal";
import type { PortfolioPosition } from "@/lib/types";
import type { FutuTradingStatus } from "@/lib/useFutuTrading";
import { parseFutuTrading } from "@/lib/useFutuTrading";

vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="modal">{children}</div>
  ),
}));

vi.mock("@/lib/futuAccountEnv", () => ({
  useFutuAccountEnv: () => "live",
  futuScopeQuery: (env: string) => `?broker=FUTU&account_env=${env}`,
}));

let tradingStatus: FutuTradingStatus | null = null;
vi.mock("@/lib/useFutuTrading", async () => {
  const actual = await vi.importActual<typeof import("@/lib/useFutuTrading")>(
    "@/lib/useFutuTrading",
  );
  return { ...actual, useFutuTrading: () => tradingStatus };
});

const onFieldEdit = vi.fn();
vi.mock("@/components/ticker-detail/useClientAttemptId", () => ({
  useClientAttemptId: () => ({
    id: "att-futu-types",
    markSubmitted: vi.fn(),
    markTerminal: vi.fn(),
    markUncertain: vi.fn(),
    isUncertain: () => false,
    uncertain: false,
    onFieldEdit,
  }),
}));

// Contract § Order types: paper default allowlist; live with every static tuple.
const PAPER_TUPLES = ["STK:LMT:DAY:RTH", "OPT:LMT:DAY:RTH", "STK:MKT:DAY:RTH"];
const LIVE_ALL_TUPLES = [
  ...["DAY", "GTC"].flatMap((tif) =>
    ["RTH", "ETH", "ALL", "OVERNIGHT"].map((s) => `STK:LMT:${tif}:${s}`),
  ),
  "STK:MKT:DAY:RTH",
  ...["STP", "STP LMT", "MIT", "LIT", "TRAIL", "TRAIL LIMIT"].flatMap((t) =>
    ["DAY", "GTC"].map((tif) => `STK:${t}:${tif}:RTH`),
  ),
  ...["LMT", "STP LMT", "LIT"].flatMap((t) =>
    ["DAY", "GTC"].map((tif) => `OPT:${t}:${tif}:RTH`),
  ),
];

function setCapabilities(caps: Record<string, unknown> | undefined) {
  tradingStatus = parseFutuTrading({
    futu: {
      trading: { paper: true, live: true },
      ...(caps ? { capabilities: caps } : {}),
    },
  });
}

const stockPos = {
  id: 1,
  ticker: "AAPL",
  structure: "Stock (100 shares)",
  structure_type: "Stock",
  risk_profile: "equity",
  expiry: "N/A",
  contracts: 100,
  direction: "LONG",
  entry_cost: 15_000,
  max_risk: null,
  market_value: 17_850,
  legs: [
    {
      direction: "LONG",
      contracts: 100,
      type: "Stock",
      strike: null,
      entry_cost: 15_000,
      avg_cost: 150,
      market_price: 178.5,
      market_value: 17_850,
      market_price_is_calculated: false,
    },
  ],
  ib_daily_pnl: null,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "",
} as unknown as PortfolioPosition;

const optionPos = {
  ...stockPos,
  id: 2,
  structure: "Long Call",
  structure_type: "Long Call",
  risk_profile: "defined",
  expiry: "2026-11-20",
  contracts: 2,
  legs: [
    {
      direction: "LONG",
      contracts: 2,
      type: "Call",
      strike: 200,
      entry_cost: 800,
      avg_cost: 4,
      market_price: 4.2,
      market_value: 840,
      market_price_is_calculated: false,
    },
  ],
} as unknown as PortfolioPosition;

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({
    ok: true,
    json: async () => ({ status: "ok", brokerOrderId: "1" }),
  });
  (globalThis as { fetch: unknown }).fetch = fetchMock;
  onFieldEdit.mockReset();
});

afterEach(() => {
  cleanup();
  tradingStatus = null;
});

function renderTicket(
  position: PortfolioPosition,
  env: "paper" | "live" = "live",
) {
  return render(
    <PositionOrderModal
      position={position}
      prices={{ AAPL: { last: 178.5, bid: 178.4, ask: 178.6 } as never }}
      onClose={() => {}}
      futuTicket={{ env, accountTail: "1234", portfolioPositions: [position] }}
    />,
  );
}

function groupButtons(
  utils: ReturnType<typeof render>,
  name: string,
): string[] {
  return within(utils.getByRole("group", { name }))
    .getAllByRole("button")
    .map((b) => b.textContent ?? "");
}

function choose(utils: ReturnType<typeof render>, group: string, label: string) {
  fireEvent.click(
    within(utils.getByRole("group", { name: group })).getByRole("button", {
      name: label,
    }),
  );
}

function fill(utils: ReturnType<typeof render>, label: RegExp, value: string) {
  fireEvent.change(utils.getByLabelText(label), { target: { value } });
}

async function submittedBody(
  utils: ReturnType<typeof render>,
): Promise<Record<string, unknown>> {
  fireEvent.click(utils.getByRole("button", { name: /^Submit/ }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/orders/place?broker=FUTU&account_env=live");
  return JSON.parse(String(init.body));
}

describe("Futu ticket — choices follow capabilities[env]", () => {
  it("paper env, stock: LMT / MKT, DAY, RTH only; no RTH checkbox", () => {
    setCapabilities({ paper: { tuples: PAPER_TUPLES, modify: ["LMT"] } });
    const utils = renderTicket(stockPos, "paper");
    expect(groupButtons(utils, "Order type")).toEqual(["LMT", "MKT"]);
    expect(groupButtons(utils, "Time in force")).toEqual(["DAY"]);
    expect(groupButtons(utils, "Session")).toEqual(["RTH"]);
    expect(utils.queryByText("FILL OUTSIDE RTH")).toBeNull();
  });

  it("live env, stock, all tuples: every type; LMT offers all sessions", () => {
    setCapabilities({ live: { tuples: LIVE_ALL_TUPLES, modify: ["LMT"] } });
    const utils = renderTicket(stockPos);
    expect(groupButtons(utils, "Order type")).toEqual([
      "LMT",
      "MKT",
      "STP",
      "STP LMT",
      "MIT",
      "LIT",
      "TRAIL",
      "TRAIL LIMIT",
    ]);
    expect(groupButtons(utils, "Time in force")).toEqual(["DAY", "GTC"]);
    expect(groupButtons(utils, "Session")).toEqual([
      "RTH",
      "ETH",
      "ALL",
      "OVERNIGHT",
    ]);
    choose(utils, "Order type", "STP");
    expect(groupButtons(utils, "Session")).toEqual(["RTH"]);
  });

  it("live env, option: LMT / STP LMT / LIT, no session selector", () => {
    setCapabilities({ live: { tuples: LIVE_ALL_TUPLES, modify: ["LMT"] } });
    const utils = renderTicket(optionPos);
    expect(groupButtons(utils, "Order type")).toEqual([
      "LMT",
      "STP LMT",
      "LIT",
    ]);
    expect(utils.queryByRole("group", { name: "Session" })).toBeNull();
  });

  it("uses the tuples of the ticket env, not the other env", () => {
    setCapabilities({
      paper: { tuples: PAPER_TUPLES, modify: ["LMT"] },
      live: { tuples: ["STK:LMT:DAY:RTH"], modify: ["LMT"] },
    });
    const utils = renderTicket(stockPos, "live");
    expect(groupButtons(utils, "Order type")).toEqual(["LMT"]);
  });

  it("no capabilities for the env → no types offered, submit disabled", () => {
    setCapabilities(undefined);
    const utils = renderTicket(stockPos);
    expect(utils.getByText(/No Futu order types are enabled/)).toBeTruthy();
    expect(
      (utils.getByRole("button", { name: /^Submit/ }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
});

describe("Futu ticket — fields per type, body built, absent fields not sent", () => {
  beforeEach(() =>
    setCapabilities({ live: { tuples: LIVE_ALL_TUPLES, modify: ["LMT"] } }),
  );

  it("LMT (default) GTC ETH: limit + session, no trigger / trail", async () => {
    const utils = renderTicket(stockPos);
    expect(utils.queryByLabelText(/Trigger Price/)).toBeNull();
    fill(utils, /Limit Price/, "179.00");
    choose(utils, "Time in force", "GTC");
    choose(utils, "Session", "ETH");
    const body = await submittedBody(utils);
    expect(body).toMatchObject({
      type: "stock",
      symbol: "AAPL",
      action: "SELL",
      quantity: 100,
      orderType: "LMT",
      limitPrice: 179,
      tif: "GTC",
      session: "ETH",
      client_attempt_id: "att-futu-types",
    });
    for (const k of ["auxPrice", "trailType", "trailValue", "trailSpread", "outsideRth"])
      expect(k in body).toBe(false);
  });

  it("MKT: no price fields shown or sent", async () => {
    const utils = renderTicket(stockPos);
    choose(utils, "Order type", "MKT");
    expect(utils.queryByLabelText(/Limit Price/)).toBeNull();
    expect(utils.queryByLabelText(/Trigger Price/)).toBeNull();
    const body = await submittedBody(utils);
    expect(body.orderType).toBe("MKT");
    expect(body.session).toBe("RTH");
    for (const k of ["limitPrice", "auxPrice", "trailValue"])
      expect(k in body).toBe(false);
  });

  it("STP: trigger only", async () => {
    const utils = renderTicket(stockPos);
    choose(utils, "Order type", "STP");
    expect(utils.queryByLabelText(/Limit Price/)).toBeNull();
    fill(utils, /Trigger Price/, "170.00");
    const body = await submittedBody(utils);
    expect(body).toMatchObject({ orderType: "STP", auxPrice: 170 });
    expect("limitPrice" in body).toBe(false);
  });

  it("STP LMT: trigger + limit; confirm text names type and trigger", async () => {
    const utils = renderTicket(stockPos);
    choose(utils, "Order type", "STP LMT");
    fill(utils, /Trigger Price/, "170.00");
    fill(utils, /Limit Price/, "169.50");
    expect(utils.getByTestId("futu-order-confirm").textContent).toBe(
      "SELL 100 AAPL · STP LMT · trigger 170.00 · limit 169.50 · DAY · RTH",
    );
    const body = await submittedBody(utils);
    expect(body).toMatchObject({
      orderType: "STP LMT",
      auxPrice: 170,
      limitPrice: 169.5,
    });
  });

  it("MIT: trigger only", async () => {
    const utils = renderTicket(stockPos);
    choose(utils, "Order type", "MIT");
    fill(utils, /Trigger Price/, "190.00");
    const body = await submittedBody(utils);
    expect(body).toMatchObject({ orderType: "MIT", auxPrice: 190 });
    expect("limitPrice" in body).toBe(false);
  });

  it("LIT: trigger + limit", async () => {
    const utils = renderTicket(stockPos);
    choose(utils, "Order type", "LIT");
    fill(utils, /Trigger Price/, "190.00");
    fill(utils, /Limit Price/, "190.50");
    const body = await submittedBody(utils);
    expect(body).toMatchObject({
      orderType: "LIT",
      auxPrice: 190,
      limitPrice: 190.5,
    });
  });

  it("TRAIL: trail type + value, no limit / trigger / spread", async () => {
    const utils = renderTicket(stockPos);
    choose(utils, "Order type", "TRAIL");
    expect(utils.queryByLabelText(/Limit Price/)).toBeNull();
    expect(utils.queryByLabelText(/Trail Spread/)).toBeNull();
    fill(utils, /Trail Value/, "1.5");
    expect(utils.getByTestId("futu-order-confirm").textContent).toContain(
      "trail 1.5%",
    );
    const body = await submittedBody(utils);
    expect(body).toMatchObject({
      orderType: "TRAIL",
      trailType: "RATIO",
      trailValue: 1.5,
    });
    for (const k of ["limitPrice", "auxPrice", "trailSpread"])
      expect(k in body).toBe(false);
  });

  it("TRAIL LIMIT: amount trail + spread, and no limit price", async () => {
    const utils = renderTicket(stockPos);
    choose(utils, "Order type", "TRAIL LIMIT");
    expect(utils.queryByLabelText(/Limit Price/)).toBeNull();
    choose(utils, "Trail type", "AMOUNT");
    fill(utils, /Trail Value/, "2.00");
    fill(utils, /Trail Spread/, "0.05");
    expect(utils.getByTestId("futu-order-confirm").textContent).toBe(
      "SELL 100 AAPL · TRAIL LIMIT · trail 2.00 · spread 0.05 · DAY · RTH",
    );
    const body = await submittedBody(utils);
    expect(body).toMatchObject({
      orderType: "TRAIL LIMIT",
      trailType: "AMOUNT",
      trailValue: 2,
      trailSpread: 0.05,
    });
    for (const k of ["limitPrice", "auxPrice"]) expect(k in body).toBe(false);
  });

  it("missing required field disables submit (no send)", () => {
    const utils = renderTicket(stockPos);
    choose(utils, "Order type", "STP");
    const submit = utils.getByRole("button", {
      name: /^Submit/,
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.click(submit);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("option STP LMT: no session in the body", async () => {
    const utils = renderTicket(optionPos);
    choose(utils, "Order type", "STP LMT");
    fill(utils, /Trigger Price/, "4.20");
    fill(utils, /Limit Price/, "4.10");
    const body = await submittedBody(utils);
    expect(body).toMatchObject({
      type: "option",
      orderType: "STP LMT",
      auxPrice: 4.2,
      limitPrice: 4.1,
    });
    expect("session" in body).toBe(false);
  });

  it("type change rotates the attempt id after a terminal reply", () => {
    const utils = renderTicket(stockPos);
    choose(utils, "Order type", "MKT");
    expect(onFieldEdit).toHaveBeenCalledWith("orderType");
  });
});

describe("IB ticket — unchanged", () => {
  it("keeps the RTH checkbox, shows no order-type selector, sends no Futu fields", async () => {
    setCapabilities({ live: { tuples: LIVE_ALL_TUPLES, modify: ["LMT"] } });
    const utils = render(
      <PositionOrderModal
        position={stockPos}
        prices={{ AAPL: { last: 178.5, bid: 178.4, ask: 178.6 } as never }}
        onClose={() => {}}
      />,
    );
    expect(utils.getByText("FILL OUTSIDE RTH")).toBeTruthy();
    expect(utils.queryByRole("group", { name: "Order type" })).toBeNull();
    expect(utils.queryByRole("group", { name: "Session" })).toBeNull();
    fireEvent.click(utils.getByRole("button", { name: /^Submit/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/orders/place");
    const body = JSON.parse(String(init.body));
    expect(body.limitPrice).toBe(178.5);
    for (const k of [
      "orderType",
      "auxPrice",
      "trailType",
      "trailValue",
      "trailSpread",
      "session",
    ])
      expect(k in body).toBe(false);
  });
});
