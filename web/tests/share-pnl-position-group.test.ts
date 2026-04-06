import { describe, expect, it } from "vitest";
import { positionGroupShareData, type PositionFillGroup } from "../components/WorkspaceSections";
import type { ExecutedOrder } from "../lib/types";

function makeOptionFill(
  overrides: Partial<ExecutedOrder> & { contract?: Partial<ExecutedOrder["contract"]> } = {},
): ExecutedOrder {
  const { contract: contractOverrides, ...rest } = overrides;
  return {
    execId: rest.execId ?? "opt-fill",
    symbol: rest.symbol ?? "AAOI",
    contract: {
      conId: 1001,
      symbol: "AAOI",
      secType: "OPT",
      strike: 90,
      right: "C",
      expiry: "2026-03-27",
      ...contractOverrides,
    },
    side: rest.side ?? "BOT",
    quantity: rest.quantity ?? 25,
    avgPrice: rest.avgPrice ?? 5.59,
    commission: rest.commission ?? -1.03,
    realizedPNL: rest.realizedPNL ?? 0,
    time: rest.time ?? "2026-03-17T15:16:13+00:00",
    exchange: rest.exchange ?? "SMART",
    ...rest,
  };
}

function makeBagFill(overrides: Partial<ExecutedOrder> = {}): ExecutedOrder {
  return {
    execId: overrides.execId ?? "bag-fill",
    symbol: overrides.symbol ?? "AAOI",
    contract: {
      conId: 2001,
      symbol: overrides.symbol ?? "AAOI",
      secType: "BAG",
      strike: 0,
      right: "?",
      expiry: null,
    },
    side: overrides.side ?? "BOT",
    quantity: overrides.quantity ?? 25,
    avgPrice: overrides.avgPrice ?? 0.25,
    commission: overrides.commission ?? 0,
    realizedPNL: overrides.realizedPNL ?? null,
    time: overrides.time ?? "2026-03-17T14:32:00+00:00",
    exchange: overrides.exchange ?? "SMART",
  };
}

describe("positionGroupShareData", () => {
  it("ignores unrelated open BAG groups and derives signed entry basis from matching opening legs", () => {
    const unrelatedOpenCombo: PositionFillGroup = {
      id: "open-unrelated-combo",
      symbol: "AAOI",
      description: "Opened AAOI Risk Reversal (Short $92 Call / Long $88 Put)",
      isClosing: false,
      totalQuantity: 25,
      netPrice: 0.25,
      totalCommission: -1.25,
      totalPnL: null,
      time: "2026-03-17T14:01:00+00:00",
      fills: [
        makeBagFill({ execId: "bag-unrelated", avgPrice: 0.25, time: "2026-03-17T14:01:00+00:00" }),
        makeOptionFill({
          execId: "call-unrelated",
          side: "BOT",
          quantity: 25,
          avgPrice: 5.10,
          realizedPNL: null,
          time: "2026-03-17T14:01:00+00:00",
          contract: { conId: 1901, strike: 92, right: "C", expiry: "2026-03-27" },
        }),
        makeOptionFill({
          execId: "put-unrelated",
          side: "SLD",
          quantity: 25,
          avgPrice: 5.35,
          realizedPNL: null,
          time: "2026-03-17T14:01:00+00:00",
          contract: { conId: 1902, strike: 88, right: "P", expiry: "2026-03-27" },
        }),
      ],
    };

    const openCallGroup: PositionFillGroup = {
      id: "open-call",
      symbol: "AAOI",
      description: "Opened AAOI Long Call",
      isClosing: false,
      totalQuantity: 25,
      netPrice: null,
      totalCommission: -17.51,
      totalPnL: null,
      time: "2026-03-17T14:14:16+00:00",
      fills: [
        makeOptionFill({
          execId: "open-call-1",
          side: "BOT",
          quantity: 12,
          avgPrice: 5.59,
          realizedPNL: null,
          time: "2026-03-17T14:14:16+00:00",
          contract: { conId: 861001, strike: 90, right: "C", expiry: "2026-03-27" },
        }),
        makeOptionFill({
          execId: "open-call-2",
          side: "BOT",
          quantity: 13,
          avgPrice: 5.59,
          realizedPNL: null,
          time: "2026-03-17T14:14:16+00:00",
          contract: { conId: 861001, strike: 90, right: "C", expiry: "2026-03-27" },
        }),
      ],
    };

    const openPutGroup: PositionFillGroup = {
      id: "open-put",
      symbol: "AAOI",
      description: "Opened AAOI Short Put",
      isClosing: false,
      totalQuantity: 25,
      netPrice: null,
      totalCommission: -17.53,
      totalPnL: null,
      time: "2026-03-17T14:12:25+00:00",
      fills: [
        makeOptionFill({
          execId: "open-put-1",
          side: "SLD",
          quantity: 13,
          avgPrice: 6.34,
          realizedPNL: null,
          time: "2026-03-17T14:12:25+00:00",
          contract: { conId: 858539, strike: 85, right: "P", expiry: "2026-03-27" },
        }),
        makeOptionFill({
          execId: "open-put-2",
          side: "SLD",
          quantity: 12,
          avgPrice: 6.34,
          realizedPNL: null,
          time: "2026-03-17T14:12:25+00:00",
          contract: { conId: 858539, strike: 85, right: "P", expiry: "2026-03-27" },
        }),
      ],
    };

    const closeGroup: PositionFillGroup = {
      id: "close-rr",
      symbol: "AAOI",
      description: "Closed AAOI Risk Reversal (Short $85 Put / Long $90 Call)",
      isClosing: true,
      totalQuantity: 25,
      netPrice: 1.0,
      totalCommission: -2.06,
      totalPnL: 4337.9,
      time: "2026-03-17T15:16:13+00:00",
      fills: [
        makeBagFill({ execId: "close-bag", avgPrice: 1.0, time: "2026-03-17T15:16:13+00:00" }),
        makeOptionFill({
          execId: "close-call",
          side: "SLD",
          quantity: 25,
          avgPrice: 5.33,
          realizedPNL: 2200,
          time: "2026-03-17T15:16:13+00:00",
          contract: { conId: 861001, strike: 90, right: "C", expiry: "2026-03-27" },
        }),
        makeOptionFill({
          execId: "close-put",
          side: "BOT",
          quantity: 25,
          avgPrice: 7.83,
          realizedPNL: 2137.9,
          time: "2026-03-17T15:16:13+00:00",
          contract: { conId: 858539, strike: 85, right: "P", expiry: "2026-03-27" },
        }),
      ],
    };

    const data = positionGroupShareData(closeGroup, [
      unrelatedOpenCombo,
      openCallGroup,
      openPutGroup,
      closeGroup,
    ]);

    expect(data.entryPrice).toBeCloseTo(-0.75, 2);
    expect(data.exitPrice).toBe(1.0);
    expect(data.pnlPct).toBeCloseTo(231.35, 2);
  });
});
