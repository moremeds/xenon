import { describe, it, expect } from "vitest";

// Types matching the production code
type OrderContract = {
  conId: number | null;
  symbol: string;
  secType: string;
  strike: number | null;
  right: string | null;
  expiry: string | null;
};

type ExecutedOrder = {
  execId: string;
  symbol: string;
  contract: OrderContract;
  side: string;
  quantity: number;
  avgPrice: number | null;
  commission: number | null;
  realizedPNL: number | null;
  time: string;
  exchange: string;
};

type BlotterTrade = {
  symbol: string;
  contract_desc: string;
  sec_type: string;
  is_closed: boolean;
  net_quantity: number;
  total_commission: number;
  realized_pnl: number;
  cost_basis: number;
  proceeds: number;
  total_cash_flow: number;
  executions: { exec_id: string; time: string; side: string; quantity: number; price: number; commission: number; notional_value: number; net_cash_flow: number }[];
};

type PositionFillGroup = {
  id: string;
  symbol: string;
  description: string;
  isClosing: boolean;
  totalQuantity: number;
  netPrice: number | null;
  totalCommission: number;
  totalPnL: number | null;
  time: string;
  fills: ExecutedOrder[];
};

// Re-implement helpers (not exported from WorkspaceSections)

function execOrderDescription(e: ExecutedOrder): string {
  const c = e.contract;
  const isClosing = e.realizedPNL != null;
  const side = e.side === "BOT"
    ? (isClosing ? "Short" : "Long")
    : e.side === "SLD"
      ? (isClosing ? "Long" : "Short")
      : e.side;
  if (c.secType === "OPT" && c.strike != null && c.right && c.expiry) {
    const right = c.right === "C" || c.right === "CALL" ? "Call" : c.right === "P" || c.right === "PUT" ? "Put" : c.right;
    return `${side} ${c.symbol} ${c.expiry} ${right} $${c.strike.toFixed(2)}`;
  }
  return `${side} ${c.symbol}`;
}

function execOrderShareData(e: ExecutedOrder) {
  return {
    description: execOrderDescription(e),
    pnl: e.realizedPNL ?? 0,
    pnlPct: e.realizedPNL != null && e.avgPrice != null && e.avgPrice > 0
      ? (e.realizedPNL / (e.avgPrice * e.quantity * (e.contract.secType === "OPT" ? 100 : 1))) * 100
      : null,
    commission: e.commission,
    fillPrice: e.avgPrice,
    entryPrice: null,
    exitPrice: null,
    time: e.time ? new Date(e.time).toLocaleString() : "",
  };
}

function positionGroupShareData(group: PositionFillGroup, allGroups?: PositionFillGroup[]) {
  let pnlPct: number | null = null;
  let entryPrice: number | null = null;

  if (group.totalPnL != null && group.isClosing) {
    const hasBagFills = group.fills.some((f) => f.contract.secType === "BAG");
    let entryNotional = 0;

    if (hasBagFills && allGroups) {
      // Only match open groups that also have BAG fills (combo opens)
      const matchingOpen = allGroups.find(
        (g) => !g.isClosing && g.symbol === group.symbol && g.netPrice != null && g.netPrice !== 0
          && g.fills.some((f) => f.contract.secType === "BAG"),
      );
      if (matchingOpen && matchingOpen.netPrice != null) {
        entryNotional = Math.abs(matchingOpen.netPrice) * matchingOpen.totalQuantity * 100;
        entryPrice = matchingOpen.netPrice;
      }

      // Fallback: derive entry from individual OPT fills across all open groups
      if (entryPrice == null) {
        const openGroups = allGroups.filter((g) => !g.isClosing && g.symbol === group.symbol);
        const openOptFills = openGroups.flatMap((g) => g.fills.filter((f) => f.contract.secType === "OPT"));
        if (openOptFills.length > 0) {
          let netCash = 0;
          for (const f of openOptFills) {
            const sign = f.side === "SLD" ? 1 : -1;
            netCash += sign * (f.avgPrice ?? 0) * f.quantity;
          }
          const comboUnits = group.totalQuantity || 1;
          entryPrice = -(netCash / comboUnits);
          if (entryNotional === 0) {
            entryNotional = Math.abs(netCash) * 100;
          }
        }
      }
    }

    if (entryNotional > 0) {
      pnlPct = (group.totalPnL / entryNotional) * 100;
    } else {
      // Fallback: use sum of closing OPT leg notionals
      const optFills = group.fills.filter((f) => f.contract.secType === "OPT");
      const totalNotional = optFills.reduce((sum, f) => {
        const mult = f.contract.secType === "OPT" ? 100 : 1;
        return sum + Math.abs((f.avgPrice ?? 0) * f.quantity * mult);
      }, 0);
      if (totalNotional > 0) {
        pnlPct = (group.totalPnL / totalNotional) * 100;
      }
    }
  }
  return {
    description: group.description,
    pnl: group.totalPnL ?? 0,
    pnlPct,
    commission: group.totalCommission,
    fillPrice: group.netPrice,
    entryPrice,
    exitPrice: group.isClosing ? group.netPrice : null,
    time: group.time ? new Date(group.time).toLocaleString() : "",
  };
}

function groupExecutedOrders(fills: ExecutedOrder[]): PositionFillGroup[] {
  if (fills.length === 0) return [];
  const cancelled = fills.filter((f) => f.side === "CANCELLED");
  const real = fills.filter((f) => f.side !== "CANCELLED");
  const groups = new Map<string, ExecutedOrder[]>();
  const isClosingFill = (f: ExecutedOrder): boolean =>
    f.contract.secType === "OPT" && f.realizedPNL != null && Math.abs(f.realizedPNL) > 0.01;

  type MinuteBucket = {
    opens: ExecutedOrder[];
    closes: ExecutedOrder[];
    bags: ExecutedOrder[];
  };

  const byMinute = new Map<string, MinuteBucket>();
  for (const fill of real) {
    const sym = fill.contract.symbol;
    const t = new Date(fill.time);
    const bucket = new Date(t.getFullYear(), t.getMonth(), t.getDate(), t.getHours(), t.getMinutes()).toISOString();
    const key = `${sym}_${bucket}`;
    if (!groups.has(key)) {
      groups.set(key, []); // backward-compat for existing helper-only behavior
    }

    const bucketData = byMinute.get(key);
    if (bucketData == null) {
      byMinute.set(key, { opens: [], closes: [], bags: [] });
    }

    const bucketBucket = byMinute.get(key);
    if (!bucketBucket) continue;

    if (fill.contract.secType === "BAG") {
      bucketBucket.bags.push(fill);
    } else if (isClosingFill(fill)) {
      bucketBucket.closes.push(fill);
    } else {
      bucketBucket.opens.push(fill);
    }
  }
  const result: PositionFillGroup[] = [];
  const nextId = (() => {
    let id = 0;
    return () => {
      id += 1;
      return `group-${id}`;
    };
  })();

  const makeGroup = (groupFills: ExecutedOrder[], isClosing: boolean): PositionFillGroup => {
    const optFills = groupFills.filter((f) => f.contract.secType !== "BAG");
    const sym = groupFills[0].contract.symbol;
    const bagFills = groupFills.filter((f) => f.contract.secType === "BAG");
    const totalQty = bagFills.length > 0
      ? bagFills.reduce((sum, f) => sum + f.quantity, 0)
      : optFills.reduce((sum, f) => sum + f.quantity, 0);
    const netPrice = bagFills.length > 0 && bagFills[0].avgPrice != null
      ? bagFills[0].avgPrice
      : null;
    const totalCommission = optFills.reduce((sum, f) => sum + (f.commission ?? 0), 0);
    const totalPnL = isClosing ? optFills.reduce((sum, f) => sum + (f.realizedPNL ?? 0), 0) : null;
    const latestTime = groupFills.reduce((maxTime, f) => {
      const current = Date.parse(f.time);
      const previous = Date.parse(maxTime);
      if (Number.isNaN(current)) return maxTime;
      if (Number.isNaN(previous)) return f.time;
      return current > previous ? f.time : maxTime;
    }, groupFills[0].time);

    result.push({
      id: nextId(),
      symbol: sym,
      description: `Test ${sym}`,
      isClosing,
      totalQuantity: totalQty,
      netPrice,
      totalCommission,
      totalPnL,
      time: latestTime,
      fills: groupFills,
    });
  };

  for (const { opens, closes, bags } of byMinute.values()) {
    const assignBagToBucket = (
      bag: ExecutedOrder,
      bucketSideFills: ExecutedOrder[],
    ): boolean => {
      if (bucketSideFills.length === 0) return closes.length === 0;
      const bagTime = Date.parse(bag.time);
      if (Number.isNaN(bagTime)) return bucketSideFills === closes;
      let bestDist = Number.POSITIVE_INFINITY;
      let nearClose = true;
      for (const fill of bucketSideFills) {
        const fillTime = Date.parse(fill.time);
        if (Number.isNaN(fillTime)) continue;
        const dist = Math.abs(fillTime - bagTime);
        if (dist < bestDist) {
          bestDist = dist;
          nearClose = bucketSideFills === closes;
        }
      }
      if (!Number.isFinite(bestDist)) return bucketSideFills === closes;
      return nearClose;
    };

    const closeBuckets: ExecutedOrder[] = [];
    const openBuckets: ExecutedOrder[] = [];

    if (opens.length > 0 && closes.length > 0) {
      for (const bag of bags) {
        const goesToClose = assignBagToBucket(bag, closes);
        if (goesToClose) closeBuckets.push(bag);
        else openBuckets.push(bag);
      }

      if (closes.length > 0 || closeBuckets.length > 0) {
        makeGroup([...closes, ...closeBuckets], true);
      }
      if (opens.length > 0 || openBuckets.length > 0) {
        makeGroup([...opens, ...openBuckets], false);
      }
    } else if (closes.length > 0) {
      makeGroup([...closes, ...bags], true);
    } else if (opens.length > 0) {
      makeGroup([...opens, ...bags], false);
    }
  }
  for (const c of cancelled) {
    result.push({
      id: c.execId, symbol: c.contract.symbol || c.symbol, description: `Cancelled ${c.symbol}`,
      isClosing: false, totalQuantity: c.quantity, netPrice: c.avgPrice, totalCommission: 0,
      totalPnL: null, time: c.time, fills: [c],
    });
  }
  result.sort((a, b) => Date.parse(b.time) - Date.parse(a.time));
  return result;
}

function blotterShareData(t: BlotterTrade) {
  const lastExec = t.executions.length > 0 ? t.executions[t.executions.length - 1] : null;
  const pnlPct = t.cost_basis !== 0 ? (t.realized_pnl / Math.abs(t.cost_basis)) * 100 : null;
  let entryPrice: number | null = null;
  let exitPrice: number | null = null;
  if (t.executions.length >= 2) {
    const firstSide = t.executions[0].side;
    const openExecs = t.executions.filter((e) => e.side === firstSide);
    const closeExecs = t.executions.filter((e) => e.side !== firstSide);
    if (openExecs.length > 0) {
      const totalQty = openExecs.reduce((s, e) => s + e.quantity, 0);
      const totalVal = openExecs.reduce((s, e) => s + e.price * e.quantity, 0);
      entryPrice = totalQty > 0 ? totalVal / totalQty : null;
    }
    if (closeExecs.length > 0) {
      const totalQty = closeExecs.reduce((s, e) => s + e.quantity, 0);
      const totalVal = closeExecs.reduce((s, e) => s + e.price * e.quantity, 0);
      exitPrice = totalQty > 0 ? totalVal / totalQty : null;
    }
  }
  return {
    description: t.contract_desc || t.symbol,
    pnl: t.realized_pnl,
    pnlPct,
    commission: t.total_commission,
    fillPrice: lastExec?.price ?? null,
    entryPrice,
    exitPrice,
    time: lastExec?.time ? new Date(lastExec.time).toLocaleString() : "",
  };
}

function cashtagTicker(desc: string): string {
  return desc.replace(
    /^(Closed|Opened|Long|Short|Bought|Sold|Cancelled)\s+([A-Z]{1,5})\b/,
    "$1 $$$2",
  );
}

function buildTweetText(description: string, pnl: number, pnlPct: number | null, showDollar: boolean, showPct: boolean): string {
  const parts: string[] = [];
  const sign = pnl >= 0 ? "+" : "-";
  const abs = Math.abs(pnl);
  if (showDollar) {
    parts.push(`${sign}$${abs.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
  }
  if (showPct && pnlPct != null && Number.isFinite(pnlPct)) {
    const pSign = pnlPct >= 0 ? "+" : "";
    parts.push(`${pSign}${pnlPct.toFixed(2)}%`);
  }
  const pnlStr = parts.join(" ");
  const tagged = cashtagTicker(description);
  return `💸 ${tagged} ${pnlStr}\n\nExecuted with Radon\n\nhttps://radon.run`;
}

// ─── Test fixtures ───────────────────────────────────────────────

function makeOptionFill(overrides: Partial<ExecutedOrder> & { contract?: Partial<OrderContract> } = {}): ExecutedOrder {
  const { contract: contractOverrides, ...rest } = overrides;
  return {
    execId: "test-1", symbol: "AAOI C92", side: "BOT", quantity: 5,
    avgPrice: 5.33, commission: -1.30, realizedPNL: 697.05,
    time: "2026-03-17T15:40:21+00:00", exchange: "PSE",
    contract: { conId: 861001, symbol: "AAOI", secType: "OPT", strike: 92, right: "C", expiry: "2026-03-27", ...contractOverrides },
    ...rest,
  };
}

function makeBagFill(overrides: Partial<ExecutedOrder> = {}): ExecutedOrder {
  return {
    execId: "bag-1", symbol: "AAOI Spread", side: "SLD", quantity: 5,
    avgPrice: 2.50, commission: 0, realizedPNL: 0,
    time: "2026-03-17T15:40:21+00:00", exchange: "SMART",
    contract: { conId: 28812380, symbol: "AAOI", secType: "BAG", strike: 0, right: "?", expiry: null },
    ...overrides,
  };
}

// ─── Position Group Share Data ───────────────────────────────────

describe("positionGroupShareData", () => {
  it("uses entry combo price for pnlPct when closing BAG group has matching open", () => {
    const openGroup: PositionFillGroup = {
      id: "open", symbol: "AAOI",
      description: "Opened AAOI Risk Reversal",
      isClosing: false, totalQuantity: 25, netPrice: 0.25,
      totalCommission: -8.50, totalPnL: null,
      time: "2026-03-17T14:32:00+00:00",
      fills: [
        makeBagFill({ quantity: 25, avgPrice: 0.25, time: "2026-03-17T14:32:00+00:00" }),
      ],
    };
    const closeGroup: PositionFillGroup = {
      id: "close", symbol: "AAOI",
      description: "Closed AAOI Risk Reversal (Short $92 Call / Long $88 Put)",
      isClosing: true, totalQuantity: 25, netPrice: 2.50,
      totalCommission: -12.50, totalPnL: 6871,
      time: "2026-03-17T15:40:21+00:00",
      fills: [
        makeBagFill({ quantity: 25 }),
        makeOptionFill({ quantity: 25, avgPrice: 5.33, realizedPNL: 3400 }),
        makeOptionFill({ quantity: 25, avgPrice: 7.83, realizedPNL: 3471, side: "SLD",
          contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 88, right: "P", expiry: "2026-03-27" },
        }),
      ],
    };
    const allGroups = [openGroup, closeGroup];
    const data = positionGroupShareData(closeGroup, allGroups);
    expect(data.pnl).toBe(6871);
    // Entry combo = $0.25 × 25 × 100 = $625
    // pnlPct = 6871 / 625 * 100 ≈ 1099.36%
    expect(data.pnlPct).toBeCloseTo(1099.36, 0);
    expect(data.description).toContain("Risk Reversal");
    expect(data.commission).toBe(-12.50);
    expect(data.fillPrice).toBe(2.50);
    expect(data.entryPrice).toBe(0.25);
    expect(data.exitPrice).toBe(2.50);
  });

  it("falls back to leg notional when no matching open group exists", () => {
    const group: PositionFillGroup = {
      id: "test", symbol: "AAOI",
      description: "Closed AAOI Risk Reversal (Short $92 Call / Long $88 Put)",
      isClosing: true, totalQuantity: 25, netPrice: 2.50,
      totalCommission: -12.50, totalPnL: 6871,
      time: "2026-03-17T15:40:21+00:00",
      fills: [
        makeBagFill({ quantity: 25 }),
        makeOptionFill({ quantity: 25, avgPrice: 5.33, realizedPNL: 3400 }),
        makeOptionFill({ quantity: 25, avgPrice: 7.83, realizedPNL: 3471, side: "SLD",
          contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 88, right: "P", expiry: "2026-03-27" },
        }),
      ],
    };
    // No allGroups passed — falls back to leg notional
    const data = positionGroupShareData(group);
    expect(data.pnl).toBe(6871);
    // notional = (5.33 * 25 * 100) + (7.83 * 25 * 100) = 13325 + 19575 = 32900
    // pnlPct = 6871 / 32900 * 100 ≈ 20.88%
    expect(data.pnlPct).toBeCloseTo(20.88, 1);
  });

  it("returns null pnlPct for opening position groups", () => {
    const group: PositionFillGroup = {
      id: "open", symbol: "AAOI",
      description: "Opened AAOI Risk Reversal",
      isClosing: false, totalQuantity: 25, netPrice: 0.25,
      totalCommission: -8.50, totalPnL: null,
      time: "2026-03-17T14:32:00+00:00",
      fills: [
        makeBagFill({ quantity: 25, avgPrice: 0.25 }),
        makeOptionFill({ quantity: 25, avgPrice: 6.72, realizedPNL: 0, side: "SLD" }),
        makeOptionFill({ quantity: 25, avgPrice: 6.47, realizedPNL: 0, side: "BOT",
          contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 88, right: "P", expiry: "2026-03-27" },
        }),
      ],
    };
    const data = positionGroupShareData(group);
    expect(data.pnl).toBe(0);
    expect(data.pnlPct).toBeNull();
  });

  it("excludes BAG fills from notional calculation", () => {
    const group: PositionFillGroup = {
      id: "test2", symbol: "AAOI",
      description: "Closed AAOI",
      isClosing: true, totalQuantity: 5, netPrice: 2.50,
      totalCommission: -1.30, totalPnL: 1375,
      time: "2026-03-17T15:40:21+00:00",
      fills: [
        makeBagFill({ quantity: 5, avgPrice: 2.50 }),
        makeOptionFill({ quantity: 5, avgPrice: 5.33, realizedPNL: 697 }),
        makeOptionFill({ quantity: 5, avgPrice: 7.83, realizedPNL: 678, side: "SLD",
          contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 88, right: "P", expiry: "2026-03-27" },
        }),
      ],
    };
    const data = positionGroupShareData(group);
    // Only OPT fills contribute to notional: (5.33*5*100) + (7.83*5*100) = 2665 + 3915 = 6580
    // pnlPct = 1375 / 6580 * 100 ≈ 20.90%
    expect(data.pnlPct).toBeCloseTo(20.90, 0);
  });

  it("handles stock-only position groups", () => {
    const group: PositionFillGroup = {
      id: "stock", symbol: "AAPL",
      description: "Closed AAPL Stock",
      isClosing: true, totalQuantity: 100, netPrice: 250,
      totalCommission: -2.00, totalPnL: 500,
      time: "2026-03-17T10:00:00+00:00",
      fills: [{
        execId: "stk-1", symbol: "AAPL", side: "SLD", quantity: 100,
        avgPrice: 252.50, commission: -2.00, realizedPNL: 500,
        time: "2026-03-17T10:00:00+00:00", exchange: "ARCA",
        contract: { conId: 100, symbol: "AAPL", secType: "STK", strike: null, right: null, expiry: null },
      }],
    };
    const data = positionGroupShareData(group);
    // No OPT fills, so totalNotional = 0, pnlPct = null
    // Stock fills aren't secType "OPT" so they're excluded from notional
    expect(data.pnlPct).toBeNull();
    expect(data.pnl).toBe(500);
  });

  it("returns null entryPrice when no matching open group and no allGroups", () => {
    const group: PositionFillGroup = {
      id: "test", symbol: "AAOI",
      description: "Closed AAOI",
      isClosing: true, totalQuantity: 5, netPrice: 2.50,
      totalCommission: -1.30, totalPnL: 1375,
      time: "2026-03-17T15:40:21+00:00",
      fills: [
        makeBagFill({ quantity: 5, avgPrice: 2.50 }),
        makeOptionFill({ quantity: 5, avgPrice: 5.33, realizedPNL: 697 }),
      ],
    };
    const data = positionGroupShareData(group);
    expect(data.entryPrice).toBeNull();
    expect(data.exitPrice).toBe(2.50);
  });

  it("derives entry from individual OPT fills when open was not a BAG (credit = negative)", () => {
    // Risk reversal: sold puts at $6.34, bought calls at $5.59 → net credit $0.75
    const openCallGroup: PositionFillGroup = {
      id: "open-call", symbol: "AAOI",
      description: "Opened AAOI Long Call",
      isClosing: false, totalQuantity: 25, netPrice: null,
      totalCommission: -17.51, totalPnL: null,
      time: "2026-03-17T14:14:16+00:00",
      fills: [
        makeOptionFill({ execId: "oc-1", quantity: 12, avgPrice: 5.59, realizedPNL: 0, side: "BOT",
          contract: { conId: 861001, symbol: "AAOI", secType: "OPT", strike: 90, right: "C", expiry: "2026-03-27" },
        }),
        makeOptionFill({ execId: "oc-2", quantity: 13, avgPrice: 5.59, realizedPNL: 0, side: "BOT",
          contract: { conId: 861001, symbol: "AAOI", secType: "OPT", strike: 90, right: "C", expiry: "2026-03-27" },
        }),
      ],
    };
    const openPutGroup: PositionFillGroup = {
      id: "open-put", symbol: "AAOI",
      description: "Opened AAOI Short Put",
      isClosing: false, totalQuantity: 25, netPrice: null,
      totalCommission: -17.53, totalPnL: null,
      time: "2026-03-17T14:12:25+00:00",
      fills: [
        makeOptionFill({ execId: "op-1", quantity: 13, avgPrice: 6.34, realizedPNL: 0, side: "SLD",
          contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 85, right: "P", expiry: "2026-03-27" },
        }),
        makeOptionFill({ execId: "op-2", quantity: 12, avgPrice: 6.34, realizedPNL: 0, side: "SLD",
          contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 85, right: "P", expiry: "2026-03-27" },
        }),
      ],
    };
    const closeGroup: PositionFillGroup = {
      id: "close", symbol: "AAOI",
      description: "Closed AAOI Risk Reversal (Short $85 Put / Long $90 Call)",
      isClosing: true, totalQuantity: 25, netPrice: 1.00,
      totalCommission: -2.06, totalPnL: 4371,
      time: "2026-03-17T15:16:13+00:00",
      fills: [
        makeBagFill({ quantity: 25, avgPrice: 1.00 }),
        makeOptionFill({ quantity: 25, avgPrice: 5.33, realizedPNL: 2200, side: "SLD",
          contract: { conId: 861001, symbol: "AAOI", secType: "OPT", strike: 90, right: "C", expiry: "2026-03-27" },
        }),
        makeOptionFill({ quantity: 25, avgPrice: 7.83, realizedPNL: 2171, side: "BOT",
          contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 85, right: "P", expiry: "2026-03-27" },
        }),
      ],
    };
    const allGroups = [openCallGroup, openPutGroup, closeGroup];
    const data = positionGroupShareData(closeGroup, allGroups);
    // Net cash from open: SELL puts (25 * 6.34 = 158.50) - BUY calls (25 * 5.59 = 139.75) = +18.75
    // Entry per combo = -(18.75 / 25) = -0.75 (credit received)
    expect(data.entryPrice).toBeCloseTo(-0.75, 2);
    expect(data.exitPrice).toBe(1.00);
    // entryNotional = |18.75| * 100 = $1875 (net credit × options multiplier)
    // pnlPct = 4371 / 1875 * 100 ≈ 233.12%
    expect(data.pnlPct).toBeCloseTo(233.12, 0);
  });

  it("returns null exitPrice for opening groups", () => {
    const group: PositionFillGroup = {
      id: "open", symbol: "AAOI",
      description: "Opened AAOI Risk Reversal",
      isClosing: false, totalQuantity: 25, netPrice: 0.25,
      totalCommission: -8.50, totalPnL: null,
      time: "2026-03-17T14:32:00+00:00",
      fills: [makeBagFill({ quantity: 25, avgPrice: 0.25 })],
    };
    const data = positionGroupShareData(group);
    expect(data.entryPrice).toBeNull();
    expect(data.exitPrice).toBeNull();
  });
});

// ─── Position Grouping ──────────────────────────────────────────

describe("groupExecutedOrders", () => {
  it("groups fills by symbol + time into position groups", () => {
    const fills: ExecutedOrder[] = [
      makeBagFill({ quantity: 5 }),
      makeOptionFill({ quantity: 5, realizedPNL: 697 }),
      makeOptionFill({ quantity: 5, avgPrice: 7.83, realizedPNL: 678, side: "SLD",
        contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 88, right: "P", expiry: "2026-03-27" },
      }),
    ];
    const groups = groupExecutedOrders(fills);
    expect(groups).toHaveLength(1);
    expect(groups[0].symbol).toBe("AAOI");
    expect(groups[0].isClosing).toBe(true);
    expect(groups[0].totalPnL).toBe(1375);
    expect(groups[0].fills).toHaveLength(3);
  });

  it("separates opening and closing fills into different groups by time", () => {
    const openFills: ExecutedOrder[] = [
      makeBagFill({ quantity: 25, avgPrice: 0.25, time: "2026-03-17T14:32:00+00:00" }),
      makeOptionFill({ quantity: 25, avgPrice: 6.72, realizedPNL: 0, side: "SLD", time: "2026-03-17T14:32:00+00:00" }),
    ];
    const closeFills: ExecutedOrder[] = [
      makeBagFill({ quantity: 25, avgPrice: 2.50, time: "2026-03-17T15:40:00+00:00" }),
      makeOptionFill({ quantity: 25, avgPrice: 5.33, realizedPNL: 3400, time: "2026-03-17T15:40:00+00:00" }),
    ];
    const groups = groupExecutedOrders([...openFills, ...closeFills]);
    expect(groups).toHaveLength(2);
    const closing = groups.find((g) => g.isClosing);
    const opening = groups.find((g) => !g.isClosing);
    expect(closing).toBeDefined();
    expect(opening).toBeDefined();
    expect(closing!.totalPnL).toBe(3400);
    expect(opening!.totalPnL).toBeNull();
  });

  it("preserves cancelled orders as standalone groups", () => {
    const cancelled: ExecutedOrder = {
      execId: "cancel-1", symbol: "GOOG Spread", side: "CANCELLED", quantity: 10,
      avgPrice: 5.00, commission: null, realizedPNL: null,
      time: "2026-03-17T12:00:00+00:00", exchange: "",
      contract: { conId: null, symbol: "GOOG", secType: "", strike: null, right: null, expiry: null },
    };
    const groups = groupExecutedOrders([cancelled, makeOptionFill()]);
    const cancelGroup = groups.find((g) => g.description.includes("Cancelled"));
    expect(cancelGroup).toBeDefined();
    expect(cancelGroup!.totalPnL).toBeNull();
  });

  it("returns empty array for empty input", () => {
    expect(groupExecutedOrders([])).toEqual([]);
  });

  it("sorts groups by latest fill timestamp, not lexical time", () => {
    const fills: ExecutedOrder[] = [
      makeBagFill({ time: "2026-03-17T10:00:00+02:00", avgPrice: 2.5, quantity: 1, side: "BOT", execId: "bag-early" }),
      makeOptionFill({
        time: "2026-03-17T10:00:00+02:00",
        quantity: 1,
        realizedPNL: 100,
        side: "SLD",
        contract: { conId: 2001, symbol: "AAOI", secType: "OPT", strike: 88, right: "C", expiry: "2026-03-27" },
      }),
      makeBagFill({ time: "2026-03-17T09:30:00+00:00", avgPrice: 2.6, quantity: 1, execId: "bag-late", symbol: "AAOI Spread" }),
      makeOptionFill({
        time: "2026-03-17T09:30:00+00:00",
        quantity: 1,
        realizedPNL: 200,
        avgPrice: 3.2,
        side: "SLD",
        contract: { conId: 2002, symbol: "AAOI", secType: "OPT", strike: 89, right: "P", expiry: "2026-03-27" },
        execId: "opt-late",
      }),
    ];

    const groups = groupExecutedOrders(fills);
    expect(groups).toHaveLength(2);
    expect(groups[0].time).toBe("2026-03-17T09:30:00+00:00");
    expect(Date.parse(groups[0].time)).toBeGreaterThan(Date.parse(groups[1].time));
  });

  it("sums quantity from BAG fills for total position size", () => {
    const fills: ExecutedOrder[] = [
      makeBagFill({ quantity: 5 }),
      makeBagFill({ execId: "bag-2", quantity: 6 }),
      makeBagFill({ execId: "bag-3", quantity: 6 }),
      makeOptionFill({ quantity: 5, realizedPNL: 697 }),
      makeOptionFill({ execId: "opt-2", quantity: 6, realizedPNL: 834 }),
      makeOptionFill({ execId: "opt-3", quantity: 6, realizedPNL: 818 }),
    ];
    const groups = groupExecutedOrders(fills);
    expect(groups).toHaveLength(1);
    expect(groups[0].totalQuantity).toBe(17); // 5 + 6 + 6 from BAG fills
  });

  it("sums commission only from OPT fills (BAG commission is always 0)", () => {
    const fills: ExecutedOrder[] = [
      makeBagFill({ quantity: 5, commission: 0 }),
      makeOptionFill({ quantity: 5, commission: -1.30, realizedPNL: 697 }),
      makeOptionFill({ quantity: 5, commission: -1.20, realizedPNL: 678, side: "SLD",
        contract: { conId: 858539, symbol: "AAOI", secType: "OPT", strike: 88, right: "P", expiry: "2026-03-27" },
      }),
    ];
    const groups = groupExecutedOrders(fills);
    expect(groups[0].totalCommission).toBeCloseTo(-2.50, 2);
  });

  it("keeps opening and closing fills in separate buckets when executed in same minute", () => {
    const fills: ExecutedOrder[] = [
      makeBagFill({ execId: "open-bag", quantity: 5, side: "BOT", avgPrice: 0.25, realizedPNL: null, time: "2026-03-17T15:40:00+00:00" }),
      makeOptionFill({
        execId: "open-opt",
        quantity: 5,
        side: "SLD",
        realizedPNL: 0,
        avgPrice: 6.72,
        time: "2026-03-17T15:40:30+00:00",
        contract: { conId: 888001, symbol: "AAOI", secType: "OPT", strike: 88, right: "P", expiry: "2026-03-27" },
      }),
      makeBagFill({ execId: "close-bag", side: "SLD", quantity: 5, avgPrice: 2.50, realizedPNL: 0, time: "2026-03-17T15:40:10+00:00" }),
      makeOptionFill({
        execId: "close-opt",
        quantity: 5,
        side: "BOT",
        realizedPNL: 687,
        avgPrice: 5.33,
        time: "2026-03-17T15:40:40+00:00",
        contract: { conId: 888002, symbol: "AAOI", secType: "OPT", strike: 89, right: "C", expiry: "2026-03-27" },
      }),
    ];

    const groups = groupExecutedOrders(fills);
    expect(groups).toHaveLength(2);
    expect(groups.some((g) => g.isClosing)).toBe(true);
    expect(groups.some((g) => !g.isClosing)).toBe(true);
    expect(groups.find((g) => g.isClosing)?.time).toBe("2026-03-17T15:40:40+00:00");
    expect(groups.find((g) => !g.isClosing)?.time).toBe("2026-03-17T15:40:30+00:00");
  });
});

// ─── Per-Fill Share Data (still used in expanded detail rows) ───

describe("execOrderDescription", () => {
  it("closing BOT → Short (was short, buying to close)", () => {
    const order = makeOptionFill({ side: "BOT", realizedPNL: 1500,
      contract: { conId: 123, symbol: "EWY", secType: "OPT", strike: 130, right: "P", expiry: "2026-03-13" },
    });
    expect(execOrderDescription(order)).toBe("Short EWY 2026-03-13 Put $130.00");
  });

  it("closing SLD → Long (was long, selling to close)", () => {
    const order = makeOptionFill({ side: "SLD", realizedPNL: 1234.56,
      contract: { conId: 456, symbol: "AAOI", secType: "OPT", strike: 45, right: "C", expiry: "2026-04-17" },
    });
    expect(execOrderDescription(order)).toBe("Long AAOI 2026-04-17 Call $45.00");
  });

  it("opening BOT → Long", () => {
    const order = makeOptionFill({ side: "BOT", realizedPNL: null });
    expect(execOrderDescription(order)).toMatch(/^Long/);
  });

  it("opening SLD → Short", () => {
    const order = makeOptionFill({ side: "SLD", realizedPNL: null });
    expect(execOrderDescription(order)).toMatch(/^Short/);
  });
});

describe("execOrderShareData", () => {
  it("computes pnlPct for option trades with 100x multiplier", () => {
    const order = makeOptionFill({ quantity: 5, avgPrice: 2.50, realizedPNL: 1250 });
    const data = execOrderShareData(order);
    expect(data.pnlPct).toBe(100); // 1250 / (2.50 * 5 * 100) * 100
  });

  it("returns null pnlPct when avgPrice is null", () => {
    const order = makeOptionFill({ avgPrice: null, realizedPNL: 100 });
    expect(execOrderShareData(order).pnlPct).toBeNull();
  });
});

// ─── Blotter Share Data ─────────────────────────────────────────

describe("blotterShareData", () => {
  it("computes share data from closed trade", () => {
    const trade: BlotterTrade = {
      symbol: "NET", contract_desc: "Long NET 2026-06-20 Call $120.00",
      sec_type: "OPT", is_closed: true, net_quantity: 0,
      total_commission: -5.20, realized_pnl: 2000, cost_basis: -4000,
      proceeds: 6000, total_cash_flow: 2000,
      executions: [
        { exec_id: "a", time: "2026-03-01T10:00:00", side: "BOT", quantity: 10, price: 4.00, commission: -2.60, notional_value: 4000, net_cash_flow: -4002.60 },
        { exec_id: "b", time: "2026-03-10T15:00:00", side: "SLD", quantity: 10, price: 6.00, commission: -2.60, notional_value: 6000, net_cash_flow: 5997.40 },
      ],
    };
    const data = blotterShareData(trade);
    expect(data.pnl).toBe(2000);
    expect(data.pnlPct).toBe(50); // 2000 / 4000 * 100
  });

  it("returns null pnlPct when cost_basis is 0", () => {
    const trade: BlotterTrade = {
      symbol: "TEST", contract_desc: "Test", sec_type: "STK",
      is_closed: true, net_quantity: 0, total_commission: 0,
      realized_pnl: 0, cost_basis: 0, proceeds: 0, total_cash_flow: 0, executions: [],
    };
    expect(blotterShareData(trade).pnlPct).toBeNull();
  });

  it("computes entry and exit prices from execution prices for OPT trades", () => {
    const trade: BlotterTrade = {
      symbol: "NET", contract_desc: "Long NET Call $120",
      sec_type: "OPT", is_closed: true, net_quantity: 0,
      total_commission: -5.20, realized_pnl: 2000, cost_basis: -4000,
      proceeds: 6000, total_cash_flow: 2000,
      executions: [
        { exec_id: "a", time: "2026-03-01T10:00:00", side: "BOT", quantity: 10, price: 4.00, commission: -2.60, notional_value: 4000, net_cash_flow: -4002.60 },
        { exec_id: "b", time: "2026-03-10T15:00:00", side: "SLD", quantity: 10, price: 6.00, commission: -2.60, notional_value: 6000, net_cash_flow: 5997.40 },
      ],
    };
    const data = blotterShareData(trade);
    // Entry = avg price of opening BOT fills = $4.00
    // Exit = avg price of closing SLD fills = $6.00
    expect(data.entryPrice).toBe(4.00);
    expect(data.exitPrice).toBe(6.00);
  });

  it("computes entry and exit prices from execution prices for STK trades", () => {
    const trade: BlotterTrade = {
      symbol: "AAPL", contract_desc: "AAPL Stock",
      sec_type: "STK", is_closed: true, net_quantity: 100,
      total_commission: -2.00, realized_pnl: 500, cost_basis: -25000,
      proceeds: 25500, total_cash_flow: 500,
      executions: [
        { exec_id: "a", time: "2026-03-01T10:00:00", side: "BOT", quantity: 100, price: 250.00, commission: -1.00, notional_value: 25000, net_cash_flow: -25001 },
        { exec_id: "b", time: "2026-03-10T15:00:00", side: "SLD", quantity: 100, price: 255.00, commission: -1.00, notional_value: 25500, net_cash_flow: 25499 },
      ],
    };
    const data = blotterShareData(trade);
    // Entry = avg price of opening BOT fills = $250.00
    // Exit = avg price of closing SLD fills = $255.00
    expect(data.entryPrice).toBe(250);
    expect(data.exitPrice).toBe(255);
  });
});

// ─── Tweet Text Builder ─────────────────────────────────────────

describe("buildTweetText", () => {
  it("includes both $ and % when both enabled", () => {
    const text = buildTweetText("Closed AAOI Risk Reversal", 6871, 20.88, true, true);
    expect(text).toContain("+$6,871.00");
    expect(text).toContain("+20.88%");
    expect(text).toContain("Closed $AAOI Risk Reversal");
    expect(text).toContain("Executed with Radon");
  });

  it("starts with 💸 emoji", () => {
    const text = buildTweetText("Closed AAOI Risk Reversal", 6871, 20.88, true, true);
    expect(text.startsWith("💸")).toBe(true);
  });

  it("prefixes ticker with $ cashtag", () => {
    const text = buildTweetText("Closed AAOI Risk Reversal (Short $92 Call / Long $88 Put)", 6871, 694.06, false, true);
    expect(text).toContain("Closed $AAOI Risk Reversal");
    // Strike prices still use $ normally
    expect(text).toContain("$92 Call");
    expect(text).toContain("$88 Put");
  });

  it("adds blank line between 'Executed with Radon' and URL", () => {
    const text = buildTweetText("Closed AAOI Risk Reversal", 6871, 20.88, false, true);
    expect(text).toContain("Executed with Radon\n\nhttps://radon.run");
  });

  it("includes only $ when showPct=false", () => {
    const text = buildTweetText("Closed AAPL Stock", 500, 2.86, true, false);
    expect(text).toContain("+$500.00");
    expect(text).not.toContain("%");
  });

  it("includes only % when showDollar=false", () => {
    const text = buildTweetText("Closed Short TSLA Put", -200, -10.5, false, true);
    expect(text).not.toContain("$200");
    expect(text).toContain("-10.50%");
  });

  it("handles negative P&L correctly", () => {
    const text = buildTweetText("Closed Short SPY Call", -567.89, -12.3, true, true);
    expect(text).toContain("-$567.89");
    expect(text).toContain("-12.30%");
  });

  it("skips % when pnlPct is null even if showPct=true", () => {
    const text = buildTweetText("Closed GOOG Spread", 100, null, true, true);
    expect(text).toContain("+$100.00");
    expect(text).not.toContain("%");
  });

  it("shows empty pnl portion when both disabled", () => {
    const text = buildTweetText("Closed X", 100, 50, false, false);
    expect(text).toContain("Closed $X");
    // Only the 💸 prefix and cashtag should be present, no dollar amounts or percentages
    expect(text).not.toMatch(/\+\$/);
    expect(text).not.toContain("%");
  });

  it("handles single-char ticker", () => {
    const text = buildTweetText("Long X 2026-04-17 Call $50.00", 500, 25.0, false, true);
    expect(text).toContain("Long $X 2026-04-17 Call $50.00");
  });

  it("does not double-tag if ticker already has $", () => {
    // If description already has $ before ticker, the regex won't match (no bare uppercase word)
    const text = buildTweetText("Some custom description", 100, 10, false, true);
    // No leading action word → no cashtag transformation
    expect(text).toContain("Some custom description");
  });
});

describe("cashtagTicker", () => {
  it("tags ticker after Closed", () => {
    expect(cashtagTicker("Closed AAOI Risk Reversal")).toBe("Closed $AAOI Risk Reversal");
  });

  it("tags ticker after Opened", () => {
    expect(cashtagTicker("Opened GOOG Bull Call Spread")).toBe("Opened $GOOG Bull Call Spread");
  });

  it("tags ticker after Long", () => {
    expect(cashtagTicker("Long AAPL 2026-04-17 Call $250.00")).toBe("Long $AAPL 2026-04-17 Call $250.00");
  });

  it("tags ticker after Short", () => {
    expect(cashtagTicker("Short SPY 2026-03-21 Put $500.00")).toBe("Short $SPY 2026-03-21 Put $500.00");
  });

  it("tags ticker after Bought", () => {
    expect(cashtagTicker("Bought MSFT")).toBe("Bought $MSFT");
  });

  it("tags ticker after Sold", () => {
    expect(cashtagTicker("Sold TSLA")).toBe("Sold $TSLA");
  });

  it("tags ticker after Cancelled", () => {
    expect(cashtagTicker("Cancelled NFLX")).toBe("Cancelled $NFLX");
  });

  it("does not tag if no leading action word", () => {
    expect(cashtagTicker("AAOI Risk Reversal")).toBe("AAOI Risk Reversal");
  });

  it("does not tag lowercase words", () => {
    expect(cashtagTicker("Closed test position")).toBe("Closed test position");
  });

  it("handles single-char tickers", () => {
    expect(cashtagTicker("Long X 2026-04-17 Call $50.00")).toBe("Long $X 2026-04-17 Call $50.00");
  });
});
