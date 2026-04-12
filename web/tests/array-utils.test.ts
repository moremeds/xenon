import { describe, it, expect } from "vitest";
import { extent, mean, bisectLeft } from "../lib/arrayUtils";

describe("extent", () => {
  it("returns [min, max] from numbers", () => {
    expect(extent([3, 1, 4, 1, 5, 9])).toEqual([1, 9]);
  });

  it("returns [min, max] with accessor", () => {
    const data = [{ v: 10 }, { v: 5 }, { v: 20 }];
    expect(extent(data, (d) => d.v)).toEqual([5, 20]);
  });

  it("returns [undefined, undefined] for empty array", () => {
    expect(extent([])).toEqual([undefined, undefined]);
  });

  it("skips null/undefined/NaN values", () => {
    const data = [
      { v: null },
      { v: 5 },
      { v: undefined },
      { v: 10 },
      { v: NaN },
    ];
    expect(extent(data, (d) => d.v as number)).toEqual([5, 10]);
  });

  it("handles single element", () => {
    expect(extent([42])).toEqual([42, 42]);
  });
});

describe("mean", () => {
  it("computes average", () => {
    expect(mean([{ v: 10 }, { v: 20 }, { v: 30 }], (d) => d.v)).toBe(20);
  });

  it("returns undefined for empty array", () => {
    expect(mean([], (d: never) => d)).toBe(undefined);
  });

  it("skips null values", () => {
    expect(
      mean([{ v: 10 }, { v: null }, { v: 30 }], (d) => d.v as number),
    ).toBe(20);
  });
});

describe("bisectLeft", () => {
  it("finds insertion point for numbers", () => {
    const arr = [{ t: 1 }, { t: 3 }, { t: 5 }, { t: 7 }];
    expect(bisectLeft(arr, 4, (d) => d.t)).toBe(2); // between 3 and 5
  });

  it("returns 0 for value before all elements", () => {
    const arr = [{ t: 10 }, { t: 20 }];
    expect(bisectLeft(arr, 5, (d) => d.t)).toBe(0);
  });

  it("returns length for value after all elements", () => {
    const arr = [{ t: 10 }, { t: 20 }];
    expect(bisectLeft(arr, 25, (d) => d.t)).toBe(2);
  });

  it("works with Date values", () => {
    const d1 = new Date("2026-01-01");
    const d2 = new Date("2026-01-03");
    const d3 = new Date("2026-01-05");
    const arr = [{ t: d1 }, { t: d2 }, { t: d3 }];
    expect(bisectLeft(arr, new Date("2026-01-02"), (d) => d.t)).toBe(1);
  });

  it("returns 0 for empty array", () => {
    expect(bisectLeft([], 5, (d: never) => d)).toBe(0);
  });
});
