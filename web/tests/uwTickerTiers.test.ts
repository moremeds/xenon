import { describe, it, expect } from "vitest";
import {
  tierOf,
  sortTier,
  groupByTier,
  isScaffold,
  makeScaffoldRow,
  mergeScaffoldWithLive,
  SCAFFOLD_ROWS,
  SCAFFOLD_TICKERS,
  MARKET_INDICES,
  COMMODITIES,
  FIXED_INCOME,
  VOLATILITY,
  SECTOR_ETFS,
} from "../lib/uwTickerTiers";
import type { UwTickerRow } from "../lib/uwAnalyzeTypes";

describe("tierOf", () => {
  it("classifies market indices", () => {
    expect(tierOf("SPY")).toBe("indices");
    expect(tierOf("QQQ")).toBe("indices");
    expect(tierOf("IWM")).toBe("indices");
    expect(tierOf("DIA")).toBe("indices");
  });
  it("classifies commodities", () => {
    expect(tierOf("GLD")).toBe("commodities");
    expect(tierOf("SLV")).toBe("commodities");
  });
  it("classifies fixed income + volatility", () => {
    expect(tierOf("TLT")).toBe("fixed");
    expect(tierOf("UVXY")).toBe("vol");
  });
  it("classifies sector ETFs", () => {
    expect(tierOf("XLK")).toBe("sector");
    expect(tierOf("SMH")).toBe("sector");
  });
  it("classifies single names", () => {
    expect(tierOf("NVDA")).toBe("single");
    expect(tierOf("aapl")).toBe("single");
  });
  it("is case-insensitive", () => {
    expect(tierOf("spy")).toBe("indices");
  });
});

describe("sortTier indices fixed order", () => {
  it("keeps SPY/QQQ/IWM/DIA in fixed order regardless of input order", () => {
    const rows = [
      { ticker: "DIA" },
      { ticker: "IWM" },
      { ticker: "QQQ" },
      { ticker: "SPY" },
    ];
    expect(sortTier(rows, "indices").map((r) => r.ticker)).toEqual([
      "SPY",
      "QQQ",
      "IWM",
      "DIA",
    ]);
  });
});

describe("sortTier single: changed-first then alphabetical", () => {
  it("puts changed rows before unchanged", () => {
    const rows = [
      { ticker: "AAPL", changes: [] },
      { ticker: "MSFT", changes: [] },
      { ticker: "TSLA", changes: [{ code: "X" }] },
    ];
    expect(sortTier(rows, "single").map((r) => r.ticker)).toEqual([
      "TSLA",
      "AAPL",
      "MSFT",
    ]);
  });
});

describe("sortTier sector is alphabetical", () => {
  it("sorts sector tickers A-Z", () => {
    const rows = [{ ticker: "XLK" }, { ticker: "XLB" }, { ticker: "SMH" }];
    expect(sortTier(rows, "sector").map((r) => r.ticker)).toEqual([
      "SMH",
      "XLB",
      "XLK",
    ]);
  });
});

describe("groupByTier", () => {
  it("splits a mixed list into all six tiers", () => {
    const rows = [
      { ticker: "NVDA" },
      { ticker: "SPY" },
      { ticker: "GLD" },
      { ticker: "TLT" },
      { ticker: "UVXY" },
      { ticker: "XLK" },
      { ticker: "XLF" },
      { ticker: "QQQ" },
    ];
    const grouped = groupByTier(rows);
    expect(grouped.indices.map((r) => r.ticker)).toEqual(["SPY", "QQQ"]);
    expect(grouped.commodities.map((r) => r.ticker)).toEqual(["GLD"]);
    expect(grouped.fixed.map((r) => r.ticker)).toEqual(["TLT"]);
    expect(grouped.vol.map((r) => r.ticker)).toEqual(["UVXY"]);
    expect(grouped.sector.map((r) => r.ticker)).toEqual(["XLF", "XLK"]);
    expect(grouped.single.map((r) => r.ticker)).toEqual(["NVDA"]);
  });
});

describe("scaffold", () => {
  it("SCAFFOLD_ROWS covers every static-universe ticker", () => {
    const expected =
      MARKET_INDICES.length +
      COMMODITIES.length +
      FIXED_INCOME.length +
      VOLATILITY.length +
      SECTOR_ETFS.length;
    expect(SCAFFOLD_ROWS.length).toBe(expected);
    expect(SCAFFOLD_TICKERS.length).toBe(expected);
  });

  it("makeScaffoldRow produces a structurally-valid UwTickerRow", () => {
    const row = makeScaffoldRow("SPY");
    expect(row.ticker).toBe("SPY");
    expect(row.sources).toEqual([]);
    expect(row.changes).toEqual([]);
    expect(row.snapshot.ticker).toBe("SPY");
    expect(row.snapshot.report.price).toBeNull();
    expect(row.snapshot.derived.gex_sign).toBeNull();
    expect(isScaffold(row)).toBe(true);
  });

  it("isScaffold returns false for rows with a real fetched_at", () => {
    const row = makeScaffoldRow("SPY");
    const populated: UwTickerRow = {
      ...row,
      snapshot: {
        ...row.snapshot,
        ts: "2026-04-08T14:02:11Z",
        report: {
          ...row.snapshot.report,
          fetched_at: "2026-04-08T14:02:11Z",
          price: 500,
        },
      },
    };
    expect(isScaffold(populated)).toBe(false);
  });
});

describe("mergeScaffoldWithLive", () => {
  const scaffold = [makeScaffoldRow("SPY"), makeScaffoldRow("QQQ")];

  function liveRow(ticker: string, price: number): UwTickerRow {
    const stub = makeScaffoldRow(ticker);
    return {
      ...stub,
      sources: ["portfolio"],
      snapshot: {
        ...stub.snapshot,
        ts: "2026-04-08T14:02:11Z",
        report: {
          ...stub.snapshot.report,
          fetched_at: "2026-04-08T14:02:11Z",
          price,
        },
      },
    };
  }

  it("prefers the live row over the scaffold stub by ticker", () => {
    const merged = mergeScaffoldWithLive(scaffold, [liveRow("SPY", 500)]);
    const spy = merged.find((r) => r.ticker === "SPY");
    expect(spy?.snapshot.report.price).toBe(500);
    expect(isScaffold(spy!)).toBe(false);
    expect(isScaffold(merged.find((r) => r.ticker === "QQQ")!)).toBe(true);
  });

  it("appends live-only ad-hoc tickers at the end", () => {
    const merged = mergeScaffoldWithLive(scaffold, [liveRow("NVDA", 800)]);
    expect(merged.map((r) => r.ticker)).toEqual(["SPY", "QQQ", "NVDA"]);
  });

  it("reverts to the scaffold stub when the live row drops off", () => {
    const afterDrop = mergeScaffoldWithLive(scaffold, []);
    expect(afterDrop.map((r) => r.ticker)).toEqual(["SPY", "QQQ"]);
    expect(afterDrop.every(isScaffold)).toBe(true);
  });
});
