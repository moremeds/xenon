import { describe, expect, it } from "vitest";
import {
  FUTU_ORDER_TYPE_FIELDS,
  buildFutuOrderTerms,
  futuOrderConfirmText,
  futuOrderTermsError,
  futuTicketChoices,
  parseFutuCapabilities,
  parseFutuTuple,
  type FutuEnvCapabilities,
  type FutuOrderType,
} from "@/lib/futuOrderTypes";
import { parseFutuTrading } from "@/lib/useFutuTrading";

// Contract docs/architecture/futu-orders.md § Order types: the paper default
// allowlist, and a live env with every static live tuple enabled.
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

function caps(tuples: string[], modify: string[] = ["LMT"]): FutuEnvCapabilities {
  const parsed = parseFutuCapabilities({ x: { tuples, modify } });
  return parsed!.x!;
}

describe("parseFutuTuple", () => {
  it("parses SEC:TYPE:TIF:SESSION, including a type with a space", () => {
    expect(parseFutuTuple("STK:STP LMT:GTC:RTH")).toEqual({
      sec: "STK",
      orderType: "STP LMT",
      tif: "GTC",
      session: "RTH",
    });
  });

  it.each([
    "STK:LMT:DAY",
    "FUT:LMT:DAY:RTH",
    "STK:TWAP:DAY:RTH",
    "STK:LMT:GTD:RTH",
    "STK:LMT:DAY:PRE",
    "STK:LMT:DAY:RTH:X",
    42,
    null,
  ])("drops malformed or unknown tuple %s", (raw) => {
    expect(parseFutuTuple(raw)).toBeNull();
  });
});

describe("parseFutuTrading — /api/health futu.capabilities", () => {
  it("reads trading flags and per-env tuples + modify types", () => {
    const status = parseFutuTrading({
      futu: {
        trading: { paper: true, live: true },
        capabilities: {
          paper: { tuples: PAPER_TUPLES, modify: ["LMT"] },
          live: { tuples: ["STK:STP:DAY:RTH", "bogus"], modify: ["LMT", "STP"] },
        },
      },
    });
    expect(status?.paper).toBe(true);
    expect(status?.capabilities.paper?.tuples).toHaveLength(3);
    expect(status?.capabilities.live?.tuples).toEqual([
      { sec: "STK", orderType: "STP", tif: "DAY", session: "RTH" },
    ]);
    expect(status?.capabilities.live?.modify).toEqual(["LMT", "STP"]);
  });

  it("missing capabilities → no env capabilities (fail closed)", () => {
    const status = parseFutuTrading({
      futu: { trading: { paper: true, live: false } },
    });
    expect(status).toEqual({ paper: true, live: false, capabilities: {} });
  });
});

describe("futuTicketChoices — choices come only from the tuples", () => {
  it("paper stock: LMT and MKT only, DAY only, RTH only", () => {
    const c = futuTicketChoices(caps(PAPER_TUPLES), "STK", {});
    expect(c.orderTypes).toEqual(["LMT", "MKT"]);
    expect(c.orderType).toBe("LMT");
    expect(c.tifs).toEqual(["DAY"]);
    expect(c.sessions).toEqual(["RTH"]);
  });

  it("paper option: LMT only", () => {
    const c = futuTicketChoices(caps(PAPER_TUPLES), "OPT", {});
    expect(c.orderTypes).toEqual(["LMT"]);
  });

  it("live all tuples, stock: every type in canonical order", () => {
    const c = futuTicketChoices(caps(LIVE_ALL_TUPLES), "STK", {});
    expect(c.orderTypes).toEqual([
      "LMT",
      "MKT",
      "STP",
      "STP LMT",
      "MIT",
      "LIT",
      "TRAIL",
      "TRAIL LIMIT",
    ]);
  });

  it("live stock LMT GTC offers every session; MKT offers DAY + RTH only", () => {
    const all = caps(LIVE_ALL_TUPLES);
    const lmt = futuTicketChoices(all, "STK", { orderType: "LMT", tif: "GTC" });
    expect(lmt.tifs).toEqual(["DAY", "GTC"]);
    expect(lmt.sessions).toEqual(["RTH", "ETH", "ALL", "OVERNIGHT"]);
    const mkt = futuTicketChoices(all, "STK", {
      orderType: "MKT",
      tif: "GTC",
      session: "ETH",
    });
    expect(mkt.tifs).toEqual(["DAY"]);
    expect(mkt.tif).toBe("DAY");
    expect(mkt.session).toBe("RTH");
  });

  it("live option: LMT, STP LMT, LIT only (never MKT / STP / TRAIL)", () => {
    const c = futuTicketChoices(caps(LIVE_ALL_TUPLES), "OPT", {});
    expect(c.orderTypes).toEqual(["LMT", "STP LMT", "LIT"]);
  });

  it("a selection outside the tuples falls back to the first allowed choice", () => {
    const c = futuTicketChoices(caps(PAPER_TUPLES), "STK", {
      orderType: "TRAIL",
      tif: "GTC",
    });
    expect(c.orderType).toBe("LMT");
    expect(c.tif).toBe("DAY");
  });

  it("one added tuple enables only that tuple", () => {
    const c = futuTicketChoices(
      caps([...PAPER_TUPLES.slice(0, 2), "STK:STP:GTC:RTH"]),
      "STK",
      { orderType: "STP" },
    );
    expect(c.orderTypes).toEqual(["LMT", "STP"]);
    expect(c.tifs).toEqual(["GTC"]);
  });

  it("no capabilities → no choices", () => {
    const c = futuTicketChoices(null, "STK", {});
    expect(c.orderTypes).toEqual([]);
    expect(c.orderType).toBeNull();
  });
});

type TermsCase = [
  FutuOrderType,
  Record<string, unknown>,
  Record<string, unknown>,
];

describe("buildFutuOrderTerms — fields per type, absent fields never sent", () => {
  const input = {
    limitPrice: 174.5,
    auxPrice: 175,
    trailType: "RATIO" as const,
    trailValue: 1.5,
    trailSpread: 0.05,
  };
  const cases: TermsCase[] = [
    ["LMT", { limitPrice: 174.5 }, {}],
    ["MKT", {}, {}],
    ["STP", { auxPrice: 175 }, {}],
    ["STP LMT", { limitPrice: 174.5, auxPrice: 175 }, {}],
    ["MIT", { auxPrice: 175 }, {}],
    ["LIT", { limitPrice: 174.5, auxPrice: 175 }, {}],
    ["TRAIL", { trailType: "RATIO", trailValue: 1.5 }, {}],
    [
      "TRAIL LIMIT",
      { trailType: "RATIO", trailValue: 1.5, trailSpread: 0.05 },
      {},
    ],
  ];

  it.each(cases)("%s", (orderType, expected) => {
    const out = buildFutuOrderTerms({
      ...input,
      orderType,
      sec: "STK",
      session: "RTH",
    });
    expect(out.ok).toBe(true);
    if (!out.ok) return;
    expect(out.fields).toEqual({ orderType, session: "RTH", ...expected });
    const f = FUTU_ORDER_TYPE_FIELDS[orderType];
    expect("limitPrice" in out.fields).toBe(f.limit);
    expect("auxPrice" in out.fields).toBe(f.aux);
    expect("trailValue" in out.fields).toBe(f.trail);
    expect("trailSpread" in out.fields).toBe(f.spread);
  });

  it("TRAIL LIMIT has no limit price", () => {
    expect(FUTU_ORDER_TYPE_FIELDS["TRAIL LIMIT"].limit).toBe(false);
  });

  it("option orders carry no session (options are RTH only)", () => {
    const out = buildFutuOrderTerms({
      ...input,
      orderType: "STP LMT",
      sec: "OPT",
      session: "RTH",
    });
    expect(out.ok && "session" in out.fields).toBe(false);
  });

  it.each([
    ["LMT", { limitPrice: NaN }],
    ["STP", { auxPrice: 0 }],
    ["TRAIL", { trailValue: -1 }],
    ["TRAIL LIMIT", { trailSpread: -0.01 }],
    ["TRAIL", { trailType: null }],
  ] as Array<[FutuOrderType, Record<string, unknown>]>)(
    "%s with a bad required field → error",
    (orderType, bad) => {
      const out = buildFutuOrderTerms({
        ...input,
        ...bad,
        orderType,
        sec: "STK",
        session: "RTH",
      } as never);
      expect(out.ok).toBe(false);
    },
  );

  it("TRAIL LIMIT accepts a zero spread", () => {
    const out = buildFutuOrderTerms({
      ...input,
      trailSpread: 0,
      orderType: "TRAIL LIMIT",
      sec: "STK",
      session: "RTH",
    });
    expect(out.ok).toBe(true);
  });
});

describe("futuOrderTermsError — route-side field table", () => {
  it("accepts LMT with a limit and no orderType (default LMT)", () => {
    expect(futuOrderTermsError({ limitPrice: 10 })).toBeNull();
  });

  it.each([
    [{ orderType: "MKT", limitPrice: 10 }, /limitPrice/],
    [{ orderType: "STP" }, /auxPrice/],
    [{ orderType: "LMT", limitPrice: 10, auxPrice: 9 }, /auxPrice/],
    [{ orderType: "TRAIL", trailType: "RATIO" }, /trailValue/],
    [
      { orderType: "TRAIL", trailType: "RATIO", trailValue: 1, trailSpread: 0 },
      /trailSpread/,
    ],
    [
      {
        orderType: "TRAIL LIMIT",
        trailType: "AMOUNT",
        trailValue: 1,
        trailSpread: 0,
        limitPrice: 5,
      },
      /limitPrice/,
    ],
  ])("%o → error", (body, re) => {
    expect(futuOrderTermsError(body)).toMatch(re);
  });
});

describe("futuOrderConfirmText — names type, trigger, trail", () => {
  it("STP LMT names trigger and limit", () => {
    const t = futuOrderConfirmText({
      action: "SELL",
      quantity: 100,
      symbol: "AAPL",
      tif: "DAY",
      fields: {
        orderType: "STP LMT",
        auxPrice: 175,
        limitPrice: 174.5,
        session: "RTH",
      },
    });
    expect(t).toBe(
      "SELL 100 AAPL · STP LMT · trigger 175.00 · limit 174.50 · DAY · RTH",
    );
  });

  it("TRAIL LIMIT names trail and spread, no limit", () => {
    const t = futuOrderConfirmText({
      action: "SELL",
      quantity: 10,
      symbol: "AAPL",
      tif: "GTC",
      fields: {
        orderType: "TRAIL LIMIT",
        trailType: "RATIO",
        trailValue: 1.5,
        trailSpread: 0.05,
        session: "RTH",
      },
    });
    expect(t).toBe(
      "SELL 10 AAPL · TRAIL LIMIT · trail 1.5% · spread 0.05 · GTC · RTH",
    );
  });

  it("TRAIL AMOUNT names the trail amount; MKT names market", () => {
    expect(
      futuOrderConfirmText({
        action: "BUY",
        quantity: 1,
        symbol: "AAPL",
        tif: "DAY",
        fields: {
          orderType: "TRAIL",
          trailType: "AMOUNT",
          trailValue: 2,
        },
      }),
    ).toBe("BUY 1 AAPL · TRAIL · trail 2.00 · DAY");
    expect(
      futuOrderConfirmText({
        action: "BUY",
        quantity: 1,
        symbol: "AAPL",
        tif: "DAY",
        fields: { orderType: "MKT", session: "RTH" },
      }),
    ).toBe("BUY 1 AAPL · MKT · market · DAY · RTH");
  });
});
