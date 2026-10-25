/**
 * X5b: /api/orders/place forwards the Futu order-type fields (orderType,
 * auxPrice, trailType, trailValue, trailSpread, session) for FUTU only.
 * An IB body never carries them: the route refuses it (400
 * INVALID_ORDER_BODY, never stripped) and the IB payload keeps its shape.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { buildFastApiPlaceOrderPayload } from "@/lib/order/placeOrderContract";
import { firstPlaceOrderSchemaErrorMessage } from "@/lib/placeOrderBodySchema";

const xenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/xenonApi")>("@/lib/xenonApi");
  return { ...actual, xenonFetch: (...args: unknown[]) => xenonFetch(...args) };
});

const FUTU_URL = "http://localhost/api/orders/place?broker=FUTU&account_env=live";
const IB_URL = "http://localhost/api/orders/place";

const base = {
  type: "stock",
  symbol: "AAPL",
  action: "SELL",
  quantity: 10,
  tif: "DAY",
  client_attempt_id: "att-x5b",
};

function post(url: string, body: unknown) {
  return new Request(url, { method: "POST", body: JSON.stringify(body) });
}

async function place(url: string, body: unknown) {
  const { POST } = await import("../app/api/orders/place/route");
  return POST(post(url, body));
}

function sentPayload(): Record<string, unknown> {
  const call = xenonFetch.mock.calls.find(([p]) =>
    String(p).startsWith("/orders/place"),
  );
  expect(call).toBeTruthy();
  return JSON.parse(String((call![1] as RequestInit).body));
}

const NEW_KEYS = [
  "orderType",
  "auxPrice",
  "trailType",
  "trailValue",
  "trailSpread",
  "session",
];

beforeEach(() => {
  xenonFetch.mockReset();
  xenonFetch.mockImplementation(async (path: string) =>
    String(path).startsWith("/orders/place")
      ? { status: "ok", brokerOrderId: "1", orderId: 0, permId: 0 }
      : {},
  );
});

describe("FUTU place — per type fields forwarded, absent fields not sent", () => {
  const cases: Array<[string, Record<string, unknown>]> = [
    ["LMT", { orderType: "LMT", limitPrice: 174.5, session: "ETH" }],
    ["MKT", { orderType: "MKT", session: "RTH" }],
    ["STP", { orderType: "STP", auxPrice: 170, session: "RTH" }],
    [
      "STP LMT",
      { orderType: "STP LMT", auxPrice: 170, limitPrice: 169.5, session: "RTH" },
    ],
    ["MIT", { orderType: "MIT", auxPrice: 190, session: "RTH" }],
    [
      "LIT",
      { orderType: "LIT", auxPrice: 190, limitPrice: 190.5, session: "RTH" },
    ],
    [
      "TRAIL",
      { orderType: "TRAIL", trailType: "RATIO", trailValue: 1.5, session: "RTH" },
    ],
    [
      "TRAIL LIMIT",
      {
        orderType: "TRAIL LIMIT",
        trailType: "AMOUNT",
        trailValue: 2,
        trailSpread: 0.05,
        session: "RTH",
      },
    ],
  ];

  it.each(cases)("%s", async (_name, terms) => {
    const res = await place(FUTU_URL, { ...base, ...terms });
    expect(res.status).toBe(200);
    const sent = sentPayload();
    expect(sent).toEqual({
      type: "stock",
      symbol: "AAPL",
      action: "SELL",
      quantity: 10,
      tif: "DAY",
      client_attempt_id: "att-x5b",
      ...terms,
    });
    expect(xenonFetch.mock.calls[0][0]).toBe(
      "/orders/place?broker=FUTU&account_env=live",
    );
  });

  it("an option order forwards its terms with no session", async () => {
    const res = await place(FUTU_URL, {
      ...base,
      type: "option",
      expiry: "20261120",
      strike: 200,
      right: "C",
      orderType: "STP LMT",
      auxPrice: 4.2,
      limitPrice: 4.1,
    });
    expect(res.status).toBe(200);
    const sent = sentPayload();
    expect(sent.orderType).toBe("STP LMT");
    expect(sent.auxPrice).toBe(4.2);
    expect(sent.limitPrice).toBe(4.1);
    expect("session" in sent).toBe(false);
  });

  it("legacy FUTU LMT body without orderType still places", async () => {
    const res = await place(FUTU_URL, { ...base, limitPrice: 178.5 });
    expect(res.status).toBe(200);
    const sent = sentPayload();
    expect(sent.limitPrice).toBe(178.5);
    for (const k of NEW_KEYS) expect(k in sent).toBe(false);
  });

  it.each([
    ["MKT with a limit", { orderType: "MKT", limitPrice: 1 }],
    ["STP without trigger", { orderType: "STP" }],
    ["LMT without limit", { orderType: "LMT" }],
    [
      "TRAIL LIMIT with a limit",
      {
        orderType: "TRAIL LIMIT",
        trailType: "RATIO",
        trailValue: 1,
        trailSpread: 0,
        limitPrice: 5,
      },
    ],
    ["unknown type", { orderType: "TWAP", limitPrice: 1 }],
    ["bad session", { orderType: "LMT", limitPrice: 1, session: "PRE" }],
  ])("%s → 400, nothing sent", async (_n, terms) => {
    const res = await place(FUTU_URL, { ...base, ...terms });
    expect(res.status).toBe(400);
    expect(xenonFetch).not.toHaveBeenCalled();
  });
});

describe("IB place — never carries the Futu fields", () => {
  it("IB body unchanged: exact payload, no new keys", async () => {
    const res = await place(IB_URL, { ...base, limitPrice: 178.5 });
    expect(res.status).toBe(200);
    expect(sentPayload()).toEqual({
      type: "stock",
      symbol: "AAPL",
      action: "SELL",
      quantity: 10,
      limitPrice: 178.5,
      tif: "DAY",
      client_attempt_id: "att-x5b",
    });
  });

  it.each(NEW_KEYS.map((k) => [k]))(
    "IB body with %s → 400 INVALID_ORDER_BODY, nothing sent",
    async (key) => {
      const value =
        key === "orderType"
          ? "LMT"
          : key === "trailType"
            ? "RATIO"
            : key === "session"
              ? "RTH"
              : 1;
      const res = await place(IB_URL, {
        ...base,
        limitPrice: 178.5,
        [key]: value,
      });
      expect(res.status).toBe(400);
      expect((await res.json()).reason_code).toBe("INVALID_ORDER_BODY");
      expect(xenonFetch).not.toHaveBeenCalled();
    },
  );

  it("IB still requires a positive limit price", async () => {
    const res = await place(IB_URL, base);
    expect(res.status).toBe(400);
    expect(xenonFetch).not.toHaveBeenCalled();
  });
});

describe("firstPlaceOrderSchemaErrorMessage — limitPrice per broker", () => {
  const noLimit = { ...base, orderType: "MKT" };
  it("IB schema still requires limitPrice", () => {
    expect(firstPlaceOrderSchemaErrorMessage(noLimit)).toMatch(/^limitPrice/);
  });
  it("FUTU schema allows an absent limitPrice", () => {
    expect(firstPlaceOrderSchemaErrorMessage(noLimit, { futu: true })).toBeNull();
  });
});

describe("buildFastApiPlaceOrderPayload — broker option", () => {
  const body = {
    ...base,
    limitPrice: 10,
    orderType: "LMT",
    session: "ETH",
  } as never;

  it("IB (default) never forwards the Futu fields", () => {
    const out = buildFastApiPlaceOrderPayload(body);
    for (const k of NEW_KEYS) expect(k in out).toBe(false);
    expect(out.limitPrice).toBe(10);
  });

  it("FUTU forwards them", () => {
    const out = buildFastApiPlaceOrderPayload(body, { futu: true });
    expect(out.orderType).toBe("LMT");
    expect(out.session).toBe("ETH");
  });

  it("FUTU omits limitPrice when the body has none", () => {
    const out = buildFastApiPlaceOrderPayload(
      { ...base, orderType: "MKT" } as never,
      { futu: true },
    );
    expect("limitPrice" in out).toBe(false);
  });
});
