import type { ExecutedOrder, PortfolioPosition } from "@/lib/types";
import { buildExecutedGroupDescription } from "@/lib/openOrderCombos";

/* ─── Share P&L helpers ────────────────────────────────── */

/** Build a human-readable description from an ExecutedOrder.
 *  e.g. "Long AAOI 2026-04-17 Call $45.00" */
/** Build a human-readable description from an ExecutedOrder.
 *  When realizedPNL is present (closing trade), show the ORIGINAL position
 *  direction: BOT closing = was Short, SLD closing = was Long.
 *  When no realizedPNL (opening trade): BOT = Long, SLD = Short. */
function execOrderDescription(e: ExecutedOrder): string {
  const c = e.contract;
  const isClosing = e.realizedPNL != null;
  const side =
    e.side === "BOT"
      ? isClosing
        ? "Short"
        : "Long"
      : e.side === "SLD"
        ? isClosing
          ? "Long"
          : "Short"
        : e.side;
  if (c.secType === "OPT" && c.strike != null && c.right && c.expiry) {
    const right =
      c.right === "C" || c.right === "CALL"
        ? "Call"
        : c.right === "P" || c.right === "PUT"
          ? "Put"
          : c.right;
    return `${side} ${c.symbol} ${c.expiry} ${right} $${c.strike.toFixed(2)}`;
  }
  return `${side} ${c.symbol}`;
}

function executedOptionContractKey(fill: ExecutedOrder): string | null {
  if (fill.contract.secType !== "OPT") return null;
  if (fill.contract.conId != null) return `conid:${fill.contract.conId}`;

  const symbol = fill.contract.symbol?.toUpperCase();
  const expiry = fill.contract.expiry?.replace(/-/g, "");
  const strike = fill.contract.strike;
  const rightRaw = fill.contract.right;
  const right = rightRaw === "CALL" ? "C" : rightRaw === "PUT" ? "P" : rightRaw;

  if (!symbol || !expiry || !right || strike == null) return null;
  return `${symbol}|${expiry}|${right}|${strike}`;
}

function resolveOpeningLegBasis(
  group: PositionFillGroup,
  allGroups?: PositionFillGroup[],
): {
  entryPrice: number | null;
  entryNotional: number;
  entryTime: string | null;
} {
  if (!allGroups)
    return { entryPrice: null, entryNotional: 0, entryTime: null };

  const closeOptFills = group.fills.filter(
    (fill) => fill.contract.secType === "OPT",
  );
  if (closeOptFills.length === 0)
    return { entryPrice: null, entryNotional: 0, entryTime: null };

  const requiredByContract = new Map<string, number>();
  for (const fill of closeOptFills) {
    const key = executedOptionContractKey(fill);
    if (!key) continue;
    requiredByContract.set(
      key,
      (requiredByContract.get(key) ?? 0) + Math.abs(fill.quantity),
    );
  }
  if (requiredByContract.size === 0)
    return { entryPrice: null, entryNotional: 0, entryTime: null };

  const closeTime = Date.parse(group.time);
  const candidateOpenFills = allGroups
    .filter(
      (candidateGroup) =>
        !candidateGroup.isClosing && candidateGroup.symbol === group.symbol,
    )
    .flatMap((candidateGroup) =>
      candidateGroup.fills.filter((fill) => fill.contract.secType === "OPT"),
    )
    .filter((fill) => {
      const key = executedOptionContractKey(fill);
      if (!key || !requiredByContract.has(key)) return false;
      const openTime = Date.parse(fill.time);
      if (
        !Number.isNaN(closeTime) &&
        !Number.isNaN(openTime) &&
        openTime > closeTime
      )
        return false;
      return true;
    })
    .sort((a, b) => {
      const aTime = Date.parse(a.time);
      const bTime = Date.parse(b.time);
      if (Number.isNaN(aTime) && Number.isNaN(bTime)) return 0;
      if (Number.isNaN(aTime)) return 1;
      if (Number.isNaN(bTime)) return -1;
      return bTime - aTime;
    });

  const remainingByContract = new Map(requiredByContract);
  let matchedQty = 0;
  let netCash = 0;
  let earliestEntryTime: string | null = null;

  for (const fill of candidateOpenFills) {
    const key = executedOptionContractKey(fill);
    if (!key) continue;

    const remainingQty = remainingByContract.get(key) ?? 0;
    if (remainingQty <= 0) continue;
    if (fill.avgPrice == null || !Number.isFinite(fill.avgPrice)) continue;

    const takeQty = Math.min(remainingQty, Math.abs(fill.quantity));
    if (takeQty <= 0) continue;

    const cashSign =
      fill.side === "SLD" || fill.side === "SELL"
        ? 1
        : fill.side === "BOT" || fill.side === "BUY"
          ? -1
          : 0;
    if (cashSign === 0) continue;

    netCash += cashSign * fill.avgPrice * takeQty;
    matchedQty += takeQty;
    remainingByContract.set(key, remainingQty - takeQty);

    // Track earliest entry time among matched fills
    if (fill.time) {
      if (!earliestEntryTime) {
        earliestEntryTime = fill.time;
      } else {
        const fillTime = Date.parse(fill.time);
        const currentEarliest = Date.parse(earliestEntryTime);
        if (
          !Number.isNaN(fillTime) &&
          !Number.isNaN(currentEarliest) &&
          fillTime < currentEarliest
        ) {
          earliestEntryTime = fill.time;
        }
      }
    }
  }

  const fullyMatched = [...remainingByContract.values()].every(
    (remainingQty) => remainingQty <= 0,
  );
  if (!fullyMatched || matchedQty <= 0)
    return { entryPrice: null, entryNotional: 0, entryTime: null };

  const comboUnits = Math.max(group.totalQuantity, 1);
  return {
    entryPrice: -(netCash / comboUnits),
    entryNotional: Math.abs(netCash) * 100,
    entryTime: earliestEntryTime,
  };
}

/** Build share data for a position group (aggregated fills).
 *  For BAG/combo closing groups, uses the matching opening group's net combo
 *  price as cost basis for accurate P&L % (e.g. risk reversal opened at $0.25
 *  credit, closed at $2.50 = +900%, not the misleading ~21% from leg notionals).
 *
 *  @param group - The position fill group to build share data for
 *  @param allGroups - All position groups (for finding matching opening fills)
 *  @param portfolioPositions - Portfolio positions (fallback for entry data when opening fills not in allGroups)
 */
export type PositionFillGroup = {
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

function deriveGroupDescription(
  fills: ExecutedOrder[],
  isClosing: boolean,
  portfolioPositions?: readonly PortfolioPosition[],
): string {
  return buildExecutedGroupDescription(fills, isClosing, portfolioPositions);
}

export function groupExecutedOrders(
  fills: ExecutedOrder[],
  portfolioPositions?: readonly PortfolioPosition[],
): PositionFillGroup[] {
  if (fills.length === 0) return [];

  // Separate cancelled orders (keep as-is, ungrouped)
  const cancelled = fills.filter((f) => f.side === "CANCELLED");
  const real = fills.filter((f) => f.side !== "CANCELLED");

  const isClosingFill = (fill: ExecutedOrder): boolean =>
    (fill.contract.secType === "OPT" || fill.contract.secType === "STK") &&
    fill.realizedPNL != null &&
    Math.abs(fill.realizedPNL) > 0.01;

  type MinuteBucket = {
    symbol: string;
    opens: ExecutedOrder[];
    closes: ExecutedOrder[];
    bags: ExecutedOrder[];
  };

  const byMinute = new Map<string, MinuteBucket>();
  for (const fill of real) {
    const sym = fill.contract.symbol;
    const t = new Date(fill.time);
    // Round time to nearest minute for grouping
    const bucket = new Date(
      t.getFullYear(),
      t.getMonth(),
      t.getDate(),
      t.getHours(),
      t.getMinutes(),
    ).toISOString();
    const key = `${sym}_${bucket}`;
    const bucketData = byMinute.get(key);
    if (bucketData == null) {
      byMinute.set(key, {
        symbol: sym,
        opens: [],
        closes: [],
        bags: [],
      });
    }

    const target = byMinute.get(key);
    if (!target) continue;

    if (fill.contract.secType === "BAG") {
      target.bags.push(fill);
    } else if (isClosingFill(fill)) {
      target.closes.push(fill);
    } else {
      target.opens.push(fill);
    }
  }

  const assignBagToBucket = (
    bag: ExecutedOrder,
    bucketSideFills: ExecutedOrder[],
    fallback: ExecutedOrder[],
  ): ExecutedOrder[] => {
    if (bucketSideFills.length === 0) return [...fallback];
    const bagTime = Date.parse(bag.time);
    const safeBagTime = Number.isNaN(bagTime) ? null : bagTime;
    let bestSide: ExecutedOrder[] = [];
    let bestDistance = Number.POSITIVE_INFINITY;

    for (const targetFill of bucketSideFills) {
      const targetTime = Date.parse(targetFill.time);
      if (Number.isNaN(targetTime) || safeBagTime == null) continue;
      const delta = Math.abs(targetTime - safeBagTime);
      if (delta < bestDistance) {
        bestDistance = delta;
        bestSide = [targetFill];
      }
    }

    if (!Number.isFinite(bestDistance)) return [...fallback];
    return bestSide;
  };

  const makeGroup = (
    groupFills: ExecutedOrder[],
    isClosing: boolean,
  ): PositionFillGroup => {
    const optFills = groupFills.filter((f) => f.contract.secType !== "BAG");
    const sym = groupFills[0].contract.symbol;
    const bagFills = groupFills.filter((f) => f.contract.secType === "BAG");
    const totalQty =
      bagFills.length > 0
        ? bagFills.reduce((sum, f) => sum + f.quantity, 0)
        : optFills.reduce((sum, f) => sum + f.quantity, 0);

    // Net price: BAG fill has the combo price, single-leg uses weighted avg
    let netPrice: number | null = null;
    if (bagFills.length > 0 && bagFills[0].avgPrice != null) {
      netPrice = bagFills[0].avgPrice;
    } else if (optFills.length > 0) {
      const totalQty = optFills.reduce((s, f) => s + f.quantity, 0);
      const weightedSum = optFills.reduce(
        (s, f) => s + (f.avgPrice ?? 0) * f.quantity,
        0,
      );
      netPrice =
        totalQty > 0 ? Number((weightedSum / totalQty).toFixed(4)) : null;
    }

    const totalCommission = optFills.reduce(
      (sum, f) => sum + (f.commission ?? 0),
      0,
    );
    const totalPnL = isClosing
      ? optFills.reduce((sum, f) => sum + (f.realizedPNL ?? 0), 0)
      : null;

    const latestTime = groupFills.reduce((maxTime, f) => {
      const current = Date.parse(f.time);
      const previous = Date.parse(maxTime);
      if (Number.isNaN(current)) return maxTime;
      if (Number.isNaN(previous)) return f.time;
      return current > previous ? f.time : maxTime;
    }, groupFills[0].time);

    return {
      id: `${sym}_${Date.parse(groupFills[0].time).toString()}`,
      symbol: sym,
      description: deriveGroupDescription(
        groupFills,
        isClosing,
        portfolioPositions,
      ),
      isClosing,
      totalQuantity: totalQty,
      netPrice,
      totalCommission,
      totalPnL,
      time: latestTime,
      fills: groupFills,
    };
  };

  const nextId = (() => {
    let id = 0;
    return () => {
      id += 1;
      return `position-group-${id}`;
    };
  })();

  const result: PositionFillGroup[] = [];
  for (const bucket of byMinute.values()) {
    const { opens, closes, bags } = bucket;
    if (opens.length > 0 && closes.length > 0) {
      const closeBuckets: ExecutedOrder[] = [];
      const openBuckets: ExecutedOrder[] = [];

      for (const bag of bags) {
        const closeDistances = assignBagToBucket(bag, closes, closes);
        const openDistances = assignBagToBucket(bag, opens, opens);
        if (closeDistances.length > 0 && openDistances.length > 0) {
          // If both have valid distances, pick the nearer side.
          const bagTime = Date.parse(bag.time);
          const closeDist = closeDistances.map((f) =>
            Math.abs(Date.parse(f.time) - bagTime),
          )[0];
          const openDist = openDistances.map((f) =>
            Math.abs(Date.parse(f.time) - bagTime),
          )[0];
          if (closeDist <= openDist) closeBuckets.push(bag);
          else openBuckets.push(bag);
        } else if (closeDistances.length > 0) {
          closeBuckets.push(bag);
        } else {
          openBuckets.push(bag);
        }
      }

      const closeGroupFills = [...closes, ...closeBuckets];
      const openGroupFills = [...opens, ...openBuckets];

      if (closeGroupFills.length > 0) {
        result.push({
          ...makeGroup(closeGroupFills, true),
          id: `${nextId()}-close`,
        });
      }
      if (openGroupFills.length > 0) {
        result.push({
          ...makeGroup(openGroupFills, false),
          id: `${nextId()}-open`,
        });
      }
      continue;
    }

    if (closes.length > 0) {
      result.push({
        ...makeGroup([...closes, ...bags], true),
        id: `${nextId()}-close`,
      });
    } else if (opens.length > 0) {
      result.push({
        ...makeGroup([...opens, ...bags], false),
        id: `${nextId()}-open`,
      });
    }
  }

  // Add cancelled orders as individual groups
  for (const c of cancelled) {
    result.push({
      id: c.execId,
      symbol: c.contract.symbol || c.symbol,
      description: `Cancelled ${c.symbol}`,
      isClosing: false,
      totalQuantity: c.quantity,
      netPrice: c.avgPrice,
      totalCommission: 0,
      totalPnL: null,
      time: c.time,
      fills: [c],
    });
  }

  // Sort by latest execution time descending
  result.sort((a, b) => {
    const bMs = Date.parse(b.time);
    const aMs = Date.parse(a.time);
    if (Number.isNaN(aMs) && Number.isNaN(bMs)) return 0;
    if (Number.isNaN(aMs)) return 1;
    if (Number.isNaN(bMs)) return -1;
    return bMs - aMs;
  });
  return result;
}

export function executedGroupDetailFills(
  group: PositionFillGroup,
): ExecutedOrder[] {
  const executableLegs = group.fills.filter(
    (fill) => fill.contract.secType !== "BAG",
  );
  return executableLegs.length > 0 ? executableLegs : group.fills;
}
