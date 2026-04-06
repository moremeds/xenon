import { describe, expect, it } from "vitest";
import {
  buildRegimeRelationshipEntries,
  summarizeRegimeRelationship,
} from "../lib/regimeRelationships";
import type { CriHistoryEntry } from "../lib/useRegime";

function point(date: string, realizedVol: number | null, cor1m: number | null): CriHistoryEntry {
  return {
    date,
    vix: 20,
    vvix: 100,
    spy: 600,
    realized_vol: realizedVol,
    cor1m: cor1m ?? undefined,
    spx_vs_ma_pct: -1,
    vix_5d_roc: 0.5,
  };
}

describe("buildRegimeRelationshipModel", () => {
  it("computes spread, z-score divergence, and quadrant state from shared RVOL/COR1M history", () => {
    const history: CriHistoryEntry[] = [
      point("2026-02-10", 12, 10),
      point("2026-02-11", 13, 11),
      point("2026-02-12", 14, 12),
      point("2026-02-13", 11, 16),
    ];

    const entries = buildRegimeRelationshipEntries(history);
    const summary = summarizeRegimeRelationship(entries);

    expect(entries).toHaveLength(4);
    expect(entries[0]?.spread).toBeCloseTo(-2, 6);
    expect(summary?.latestSpread).toBeCloseTo(5, 6);
    expect(summary?.latestQuadrant).toBe("Fragile Calm");
    expect(summary?.latestDivergence).toBeGreaterThan(0);
  });

  it("replaces the latest history point with live values before computing the relationship model", () => {
    const history: CriHistoryEntry[] = [
      point("2026-02-10", 12, 10),
      point("2026-02-11", 13, 11),
      point("2026-02-12", 14, 12),
      point("2026-02-13", 11, 16),
    ];

    const entries = buildRegimeRelationshipEntries(history, {
      realized_vol: 10,
      cor1m: 18,
    });
    const summary = summarizeRegimeRelationship(entries);

    expect(entries.at(-1)?.realizedVol).toBeCloseTo(10, 6);
    expect(entries.at(-1)?.cor1m).toBeCloseTo(18, 6);
    expect(summary?.latestSpread).toBeCloseTo(8, 6);
    expect(summary?.latestQuadrant).toBe("Fragile Calm");
  });
});
