/**
 * X5b Task 2 (Astra A6): the Futu modify path carries the trigger / trail
 * terms. Helpers: change detection per order type, lock snapshot covering the
 * new terms, modify gating by capabilities. Route: newAuxPrice /
 * newTrailValue / newTrailSpread are validated and forwarded for FUTU only;
 * an IB body carrying one is refused (400 INVALID_ORDER_BODY, nothing sent).
 * All symbols, prices and broker responses are synthetic.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  buildFutuModifyRequest,
  futuModifyConfirmText,
  futuModifyFieldsError,
  futuModifyTermsText,
  FUTU_MODIFY_NO_CHANGE,
} from "@/lib/futuOrderTypes";
import {
  futuLockStillHolds,
  futuManageableOpenOrder,
  futuManagementLockFor,
  futuModifiableOpenOrder,
} from "@/lib/futuOpenOrder";
import { XenonApiError } from "@/lib/xenonApi";
import type { OpenOrder } from "@/lib/types";

const xenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/xenonApi")>("@/lib/xenonApi");
  return { ...actual, xenonFetch: (...args: unknown[]) => xenonFetch(...args) };
});

const row = (over: Partial<OpenOrder>): OpenOrder =>
  ({
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
    totalQuantity: 10,
    remaining: 10,
    filled: 0,
    limitPrice: 10,
    auxPrice: null,
    trailType: null,
    trailValue: null,
    trailSpread: null,
    session: "RTH",
    tif: "DAY",
    status: "Submitted",
    ...over,
  }) as unknown as OpenOrder;

const NAN = Number.NaN;
const input = (over: Partial<Record<string, number>> = {}) => ({
  quantity: 10,
  limitPrice: NAN,
  auxPrice: NAN,
  trailValue: NAN,
  trailSpread: NAN,
  ...over,
});

describe("buildFutuModifyRequest — only changed fields of the order type", () => {
  it("STP aux-only: newAuxPrice alone", () => {
    const o = row({ orderType: "STP", limitPrice: null, auxPrice: 9.5 });
    const r = buildFutuModifyRequest(o, input({ auxPrice: 9.25 }));
    expect(r).toEqual({
      ok: true,
      request: { newAuxPrice: 9.25 },
      changes: [{ field: "trigger", from: 9.5, to: 9.25 }],
    });
  });

  it("STP LMT: trigger and limit together, unchanged qty omitted", () => {
    const o = row({ orderType: "STP LMT", limitPrice: 9.4, auxPrice: 9.5 });
    const r = buildFutuModifyRequest(
      o,
      input({ limitPrice: 9.3, auxPrice: 9.4 }),
    );
    expect(r.ok && r.request).toEqual({ newPrice: 9.3, newAuxPrice: 9.4 });
  });

  it("TRAIL trail-only: newTrailValue alone", () => {
    const o = row({
      orderType: "TRAIL",
      limitPrice: null,
      trailType: "RATIO",
      trailValue: 2,
    });
    const r = buildFutuModifyRequest(o, input({ trailValue: 2.5 }));
    expect(r.ok && r.request).toEqual({ newTrailValue: 2.5 });
  });

  it("TRAIL LIMIT spread-only, and a zero spread is a valid value", () => {
    const o = row({
      orderType: "TRAIL LIMIT",
      limitPrice: null,
      trailType: "AMOUNT",
      trailValue: 0.5,
      trailSpread: 0.05,
    });
    const r = buildFutuModifyRequest(
      o,
      input({ trailValue: 0.5, trailSpread: 0 }),
    );
    expect(r.ok && r.request).toEqual({ newTrailSpread: 0 });
  });

  it("fields the type does not have are never sent", () => {
    const o = row({ orderType: "MKT", limitPrice: null });
    const r = buildFutuModifyRequest(
      o,
      input({ quantity: 5, limitPrice: 9, auxPrice: 9, trailValue: 1 }),
    );
    expect(r.ok && r.request).toEqual({ newQuantity: 5 });
  });

  it("no change → not ok (no send)", () => {
    const o = row({ orderType: "STP", limitPrice: null, auxPrice: 9.5 });
    expect(buildFutuModifyRequest(o, input({ auxPrice: 9.5 }))).toEqual({
      ok: false,
      message: FUTU_MODIFY_NO_CHANGE,
    });
  });

  it("invalid shown field → not ok", () => {
    const o = row({ orderType: "STP", limitPrice: null, auxPrice: 9.5 });
    const r = buildFutuModifyRequest(o, input({ auxPrice: -1 }));
    expect(r.ok).toBe(false);
    const blank = buildFutuModifyRequest(o, input({ auxPrice: NAN }));
    expect(blank.ok).toBe(false);
  });

  it("unknown order type cannot be modified here", () => {
    const o = row({ orderType: "WEIRD" });
    expect(buildFutuModifyRequest(o, input({ quantity: 5 })).ok).toBe(false);
  });

  it("confirm text and toast text name the new terms", () => {
    expect(
      futuModifyConfirmText([
        { field: "trigger", from: 9.5, to: 9.25 },
        { field: "qty", from: 10, to: 5 },
      ]),
    ).toBe("trigger 9.50 → 9.25 · qty 10 → 5");
    expect(
      futuModifyConfirmText([{ field: "trail", from: 2, to: 2.5 }], "RATIO"),
    ).toBe("trail 2% → 2.5%");
    expect(
      futuModifyTermsText({ newAuxPrice: 9.25, newTrailSpread: 0.05 }),
    ).toBe("trigger 9.25, spread 0.05");
  });
});

describe("lock snapshot covers trigger / trail terms (A6)", () => {
  const o = row({
    orderType: "TRAIL LIMIT",
    limitPrice: null,
    trailType: "AMOUNT",
    trailValue: 0.5,
    trailSpread: 0.05,
    auxPrice: 9.5,
  });
  const lock = futuManagementLockFor("live", {
    ...o,
    brokerOrderId: o.brokerOrderId!,
  });

  it("same terms hold the lock", () => {
    expect(futuLockStillHolds(lock, { ...o })).toBe(true);
  });
  it.each([
    ["auxPrice", 9.4],
    ["trailValue", 0.6],
    ["trailSpread", 0.04],
  ])("a refreshed row with a changed %s releases it", (key, value) => {
    expect(futuLockStillHolds(lock, { ...o, [key]: value })).toBe(false);
  });
});

describe("modify offered only for capabilities.modify types; cancel any type", () => {
  const caps = { tuples: [], modify: ["LMT" as const] };
  it("STP row: cancellable, not modifiable with modify=[LMT]", () => {
    const stp = row({ orderType: "STP", limitPrice: null, auxPrice: 9 });
    expect(futuManageableOpenOrder(stp)).toBe(true);
    expect(futuModifiableOpenOrder(stp, caps)).toBe(false);
    expect(
      futuModifiableOpenOrder(stp, { tuples: [], modify: ["LMT", "STP"] }),
    ).toBe(true);
  });
  it("LMT row: modifiable; no capabilities → not modifiable", () => {
    expect(futuModifiableOpenOrder(row({}), caps)).toBe(true);
    expect(futuModifiableOpenOrder(row({}), null)).toBe(false);
  });
});

describe("futuModifyFieldsError", () => {
  it("accepts positive aux / trail and a zero spread", () => {
    expect(
      futuModifyFieldsError({
        newAuxPrice: 1,
        newTrailValue: 2,
        newTrailSpread: 0,
      }),
    ).toBeNull();
  });
  it.each([
    [{ newAuxPrice: 0 }],
    [{ newTrailValue: -1 }],
    [{ newTrailSpread: -0.01 }],
    [{ newAuxPrice: "9" }],
  ])("refuses %o", (body) => {
    expect(futuModifyFieldsError(body)).toMatch(/must be/);
  });
});

/* ── Route ────────────────────────────────────────────────────────────── */

const FUTU_URL =
  "http://localhost/api/orders/modify?broker=FUTU&account_env=live";
const IB_URL = "http://localhost/api/orders/modify";

async function modify(url: string, body: unknown) {
  const { POST } = await import("../app/api/orders/modify/route");
  return POST(
    new Request(url, { method: "POST", body: JSON.stringify(body) }),
  );
}

function sentModify(): Record<string, unknown> {
  const call = xenonFetch.mock.calls.find(([p]) =>
    String(p).startsWith("/orders/modify"),
  );
  expect(call).toBeTruthy();
  return JSON.parse(String((call![1] as RequestInit).body));
}

const sentModifyCount = () =>
  xenonFetch.mock.calls.filter(([p]) => String(p).startsWith("/orders/modify"))
    .length;

describe("/api/orders/modify — Futu trigger / trail terms", () => {
  beforeEach(() => {
    xenonFetch.mockReset();
    xenonFetch.mockImplementation(async (path: string) => {
      if (String(path).startsWith("/orders/modify"))
        return { status: "ok", confirmed: false, applied_sequence: 2 };
      if (String(path).startsWith("/orders?"))
        return { open_orders: [], executed_orders: [] };
      return {};
    });
  });

  it("aux-only: forwarded, confirmed:false passed through", async () => {
    const res = await modify(FUTU_URL, {
      brokerOrderId: "9001",
      newAuxPrice: 9.25,
      modifySequence: 2,
    });
    expect(res.status).toBe(200);
    const json = await res.json();
    expect(json.confirmed).toBe(false);
    expect(sentModify()).toEqual({
      brokerOrderId: "9001",
      newAuxPrice: 9.25,
      modifySequence: 2,
    });
  });

  it("trail-only and spread-only (0) are forwarded", async () => {
    await modify(FUTU_URL, {
      brokerOrderId: "9001",
      newTrailValue: 2.5,
      newTrailSpread: 0,
      modifySequence: 3,
    });
    expect(sentModify()).toEqual({
      brokerOrderId: "9001",
      newTrailValue: 2.5,
      newTrailSpread: 0,
      modifySequence: 3,
    });
  });

  it("FUTU body with no modify field → 400, nothing sent", async () => {
    const res = await modify(FUTU_URL, {
      brokerOrderId: "9001",
      outsideRth: true,
      modifySequence: 2,
    });
    expect(res.status).toBe(400);
    expect((await res.json()).reason_code).toBe("INVALID_ORDER_BODY");
    expect(sentModifyCount()).toBe(0);
  });

  it("FUTU invalid trigger → 400, nothing sent", async () => {
    const res = await modify(FUTU_URL, {
      brokerOrderId: "9001",
      newAuxPrice: 0,
    });
    expect(res.status).toBe(400);
    expect((await res.json()).reason_code).toBe("INVALID_ORDER_BODY");
    expect(sentModifyCount()).toBe(0);
  });

  it.each([["newAuxPrice"], ["newTrailValue"], ["newTrailSpread"]])(
    "IB body with %s → 400 INVALID_ORDER_BODY, nothing sent",
    async (key) => {
      const res = await modify(IB_URL, {
        orderId: 10,
        permId: 20,
        newPrice: 11,
        [key]: 1,
      });
      expect(res.status).toBe(400);
      expect((await res.json()).reason_code).toBe("INVALID_ORDER_BODY");
      expect(sentModifyCount()).toBe(0);
    },
  );

  it("IB modify body unchanged (no Futu keys)", async () => {
    xenonFetch.mockImplementation(async (path: string) => {
      if (String(path).startsWith("/orders/modify")) return { status: "ok" };
      if (String(path).startsWith("/orders"))
        return {
          open_orders: [{ orderId: 10, permId: 20, limitPrice: 11, totalQuantity: 1 }],
          executed_orders: [],
        };
      return {};
    });
    const res = await modify(IB_URL, {
      orderId: 10,
      permId: 20,
      newPrice: 11,
      modifySequence: 2,
    });
    expect(res.status).toBe(200);
    expect(sentModify()).toEqual({
      orderId: 10,
      permId: 20,
      newPrice: 11,
      modifySequence: 2,
    });
  });

  it("uncertain upstream reply keeps its reason code", async () => {
    xenonFetch.mockImplementation(async (path: string) => {
      if (String(path).startsWith("/orders/modify"))
        throw new XenonApiError(502, "uncertain", {
          reason_code: "FUTU_SEND_UNCERTAIN",
        });
      return {};
    });
    const res = await modify(FUTU_URL, {
      brokerOrderId: "9001",
      newAuxPrice: 9.25,
    });
    expect(res.status).toBe(502);
    expect((await res.json()).reason_code).toBe("FUTU_SEND_UNCERTAIN");
  });
});
