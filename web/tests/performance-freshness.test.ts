import { describe, expect, it } from "vitest";
import {
  isPerformanceBehindPortfolioSync,
  isPortfolioBehindCurrentEtSession,
  latestPortfolioTargetDateET,
  portfolioAsOfFromLastSync,
} from "../lib/performanceFreshness";

describe("performance freshness", () => {
  it("derives the portfolio session date from last_sync", () => {
    expect(portfolioAsOfFromLastSync("2026-03-13T21:00:00Z")).toBe("2026-03-13");
    expect(portfolioAsOfFromLastSync(null)).toBeNull();
  });

  it("marks performance as behind when portfolio sync advances after the panel loads", () => {
    expect(isPerformanceBehindPortfolioSync(
      {
        as_of: "2026-03-12",
        last_sync: "2026-03-12T20:59:00Z",
      },
      "2026-03-13T21:01:00Z",
    )).toBe(true);
  });

  it("treats matching sync timestamps and session date as current", () => {
    expect(isPerformanceBehindPortfolioSync(
      {
        as_of: "2026-03-13",
        last_sync: "2026-03-13T21:01:00Z",
      },
      "2026-03-13T21:01:00Z",
    )).toBe(false);
  });

  it("targets the latest weekday in ET for portfolio freshness checks", () => {
    expect(latestPortfolioTargetDateET(new Date("2026-03-13T16:10:00Z"))).toBe("2026-03-13");
    expect(latestPortfolioTargetDateET(new Date("2026-03-14T16:10:00Z"))).toBe("2026-03-13");
  });

  it("marks a portfolio snapshot as behind when it still points at a prior ET session", () => {
    expect(isPortfolioBehindCurrentEtSession("2026-03-12T13:23:21Z", "2026-03-13")).toBe(true);
    expect(isPortfolioBehindCurrentEtSession("2026-03-13T13:23:21Z", "2026-03-13")).toBe(false);
  });
});
