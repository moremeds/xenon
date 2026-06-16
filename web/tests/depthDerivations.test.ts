import { describe, it, expect } from "vitest";
import {
  groupPriceLevels,
  montageFill,
  buildLadderRows,
  classifyTicks,
  isBestLevel,
  deriveBookHeader,
} from "@/lib/book/depthDerivations";
import type { DepthLevel } from "@/lib/pricesProtocol";

const lvl = (
  price: number,
  size: number,
  mm: string | null = null,
): DepthLevel => ({ price, size, marketMaker: mm, exchange: mm });

describe("depthDerivations", () => {
  it("groupPriceLevels flags first row of each distinct price", () => {
    const r = groupPriceLevels([lvl(100, 1), lvl(100, 2), lvl(99, 3)]);
    expect(r.map((x) => x.firstOfLevel)).toEqual([true, false, true]);
  });

  it("montageFill is size/maxSize, 0 when maxSize<=0", () => {
    expect(montageFill(lvl(100, 50), 100)).toBe(0.5);
    expect(montageFill(lvl(100, 50), 0)).toBe(0);
  });

  it("buildLadderRows pads to fixed rows per side, best adjacent to spine", () => {
    const { askRows, bidRows } = buildLadderRows(
      { bid: [lvl(99, 10)], ask: [lvl(101, 20)] },
      3,
    );
    expect(askRows.length).toBe(3);
    expect(bidRows.length).toBe(3);
    expect(askRows[askRows.length - 1].level?.price).toBe(101); // best ask just above spine
    expect(bidRows[0].level?.price).toBe(99); // best bid just below spine
  });

  it("classifyTicks applies the tick test (first is flat)", () => {
    const t = classifyTicks([
      { price: 10, size: 1, exchange: null, time: "1" },
      { price: 11, size: 1, exchange: null, time: "2" },
      { price: 11, size: 1, exchange: null, time: "3" },
    ]);
    expect(t.map((x) => x.tone)).toEqual(["flat", "up", "flat"]);
  });

  it("isBestLevel: index 0 for stock/future, nbbo flag for option", () => {
    expect(isBestLevel(lvl(100, 1), 0, "stock")).toBe(true);
    expect(isBestLevel({ ...lvl(100, 1), nbbo: true }, 3, "option")).toBe(true);
  });

  it("deriveBookHeader falls back to L1 when no entitled book", () => {
    const h = deriveBookHeader(null, {
      bid: 1,
      ask: 2,
      last: 1.5,
      lastLabel: "LAST",
    });
    expect(h).toEqual({ bid: 1, ask: 2, last: 1.5, lastLabel: "LAST" });
  });
});
