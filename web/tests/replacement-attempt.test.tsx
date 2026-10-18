/**
 * @vitest-environment jsdom
 *
 * PR-07 fix2 (OP-1): a combo replacement (cancel original + place successor)
 * carries one client_attempt_id per logical replacement, keyed to the target
 * order, owned by OrderActionsContext. A response that does not prove the
 * outcome (fetch rejection, unreadable body, uncoded 5xx, PLACE_OUTCOME_UNKNOWN)
 * locks that replacement: no retry fetch, "status unknown" copy.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import React from "react";
import { renderHook, act, render, cleanup, fireEvent } from "@testing-library/react";
import {
  OrderActionsProvider,
  useOrderActions,
} from "@/lib/OrderActionsContext";
import ModifyOrderModal from "@/components/ModifyOrderModal";
import { REPLACEMENT_UNCERTAIN_COPY } from "@/lib/order/submitPlace";
import type { OpenOrder } from "@/lib/types";
import type { ReplaceComboOrder } from "@/lib/orderModify";

vi.mock("@/components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="modal">{children}</div>
  ),
}));

const wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(OrderActionsProvider, null, children);

function comboOrder(permId: number): OpenOrder {
  return {
    orderId: permId - 1000,
    permId,
    symbol: "SPY spread",
    action: "BUY",
    orderType: "LMT",
    tif: "DAY",
    totalQuantity: 1,
    limitPrice: 1.5,
    auxPrice: null,
    status: "Submitted",
    contract: {
      symbol: "SPY",
      secType: "BAG",
      strike: null,
      right: null,
      expiry: null,
      comboLegs: [
        {
          action: "BUY",
          symbol: "SPY",
          expiry: "20260619",
          strike: 200,
          right: "C",
          ratio: 1,
        },
        {
          action: "SELL",
          symbol: "SPY",
          expiry: "20260619",
          strike: 210,
          right: "C",
          ratio: 1,
        },
      ],
    },
  } as unknown as OpenOrder;
}

const replaceOrder: ReplaceComboOrder = {
  type: "combo",
  symbol: "SPY",
  action: "BUY",
  quantity: 1,
  limitPrice: 1.5,
  tif: "DAY",
  legs: [
    { expiry: "20260619", strike: 200, right: "C", action: "BUY", ratio: 1 },
    { expiry: "20260619", strike: 215, right: "C", action: "SELL", ratio: 1 },
  ],
};

const okJson = (body: unknown) => ({
  ok: true,
  status: 200,
  json: async () => body,
});
type FetchMock = ReturnType<typeof vi.fn>;
/** Stub fetch: POST /api/orders (panel refresh) answers an empty book;
 * every other URL goes to `modify`. */
function stubFetch(modify: (...args: unknown[]) => Promise<unknown>): FetchMock {
  const fetchMock = vi.fn((url: unknown, init?: unknown) =>
    url === "/api/orders"
      ? Promise.resolve(okJson({ open_orders: [], executed_orders: [] }))
      : modify(url, init),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
const modifyCalls = (fetchMock: FetchMock) =>
  fetchMock.mock.calls.filter((c) => c[0] === "/api/orders/modify");
const sentAttemptId = (fetchMock: FetchMock, call: number) =>
  JSON.parse(String((modifyCalls(fetchMock)[call][1] as RequestInit).body))
    .replaceOrder.client_attempt_id as string;

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

describe("combo replacement attempt id — one per logical replacement", () => {
  it("two consecutive replacements on different orders each get a fresh id and both resolve", async () => {
    const fetchMock = stubFetch(() => Promise.resolve(okJson({ status: "ok" })));
    const { result } = renderHook(() => useOrderActions(), { wrapper });

    let a: unknown, b: unknown;
    await act(async () => {
      a = await result.current.requestModify(comboOrder(9001), {
        replaceOrder,
      });
    });
    await act(async () => {
      b = await result.current.requestModify(comboOrder(9002), {
        replaceOrder,
      });
    });
    expect(a).toEqual({ ok: true });
    expect(b).toEqual({ ok: true });
    expect(modifyCalls(fetchMock)).toHaveLength(2);
    // The replace response carries no orders: the panel is refreshed.
    expect(fetchMock.mock.calls.some((c) => c[0] === "/api/orders")).toBe(true);
    const [idA, idB] = [
      sentAttemptId(fetchMock, 0),
      sentAttemptId(fetchMock, 1),
    ];
    expect(idA).toBeTruthy();
    expect(idB).toBeTruthy();
    expect(idA).not.toBe(idB);
  });

  it("a resolved replacement drops its id: the next replacement of the same order mints a new one", async () => {
    const modify = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 400,
        json: async () => ({ error: "bad", reason_code: "INVALID_ORDER_BODY" }),
      })
      .mockResolvedValueOnce(okJson({ status: "ok" }));
    const fetchMock = stubFetch(modify);
    const { result } = renderHook(() => useOrderActions(), { wrapper });

    let first: { ok: boolean } | undefined;
    await act(async () => {
      first = (await result.current.requestModify(comboOrder(9001), {
        replaceOrder,
      })) as { ok: boolean };
    });
    expect(first?.ok).toBe(false);
    expect(result.current.lockedReplacements.has(9001)).toBe(false);
    await act(async () => {
      await result.current.requestModify(comboOrder(9001), { replaceOrder });
    });
    expect(modifyCalls(fetchMock)).toHaveLength(2);
    expect(sentAttemptId(fetchMock, 0)).not.toBe(sentAttemptId(fetchMock, 1));
  });
});

describe("combo replacement response — unproven outcome is uncertain and locked", () => {
  it.each([
    [
      "an unreadable 200",
      () =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: async () => {
            throw new SyntaxError("bad json");
          },
        }),
    ],
    [
      "an uncoded 502",
      () =>
        Promise.resolve({
          ok: false,
          status: 502,
          json: async () => ({ error: "Bad Gateway" }),
        }),
    ],
    [
      "PLACE_OUTCOME_UNKNOWN",
      () =>
        Promise.resolve({
          ok: false,
          status: 502,
          json: async () => ({ reason_code: "PLACE_OUTCOME_UNKNOWN" }),
        }),
    ],
    [
      "a fetch rejection",
      () => Promise.reject(new TypeError("Failed to fetch")),
    ],
  ])(
    "%s → status unknown, form locked, no retry fetch",
    async (_label, respond) => {
      const fetchMock = stubFetch(respond);
      const { result } = renderHook(() => useOrderActions(), { wrapper });

      let res:
        | { ok: boolean; reasonCode?: string | null; message?: string }
        | undefined;
      await act(async () => {
        res = (await result.current.requestModify(comboOrder(9001), {
          replaceOrder,
        })) as typeof res;
      });
      expect(res).toMatchObject({
        ok: false,
        reasonCode: "PLACE_OUTCOME_UNKNOWN",
        message: REPLACEMENT_UNCERTAIN_COPY,
      });
      expect(res?.message).not.toMatch(/replaced/);
      expect(result.current.lockedReplacements.has(9001)).toBe(true);
      const notes = result.current.drainNotifications();
      expect(notes.some((n) => n.type === "success")).toBe(false);
      expect(notes.some((n) => n.message === REPLACEMENT_UNCERTAIN_COPY)).toBe(
        true,
      );

      await act(async () => {
        res = (await result.current.requestModify(comboOrder(9001), {
          replaceOrder,
        })) as typeof res;
      });
      expect(res?.reasonCode).toBe("PLACE_OUTCOME_UNKNOWN");
      expect(modifyCalls(fetchMock)).toHaveLength(1);
    },
  );

  it("ModifyOrderModal locked: a leg restructure cannot be submitted and the status-unknown copy shows", () => {
    const onConfirm = vi.fn();
    const { getByRole, getByText, getByLabelText } = render(
      <ModifyOrderModal order={comboOrder(9001)} loading={false} onConfirm={onConfirm} onClose={() => {}} locked />,
    );
    fireEvent.change(getByLabelText(/Strike/i, { selector: "#modify-leg-1-strike" }), { target: { value: "215" } });
    const button = getByRole("button", { name: /Modify Order/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(getByText(REPLACEMENT_UNCERTAIN_COPY)).toBeTruthy();
  });
});
