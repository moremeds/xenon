import { describe, expect, it } from "vitest";
import {
  buildRegimeRelationshipEntries,
  summarizeRegimeRelationship,
  type RegimeRelationshipSource,
} from "../lib/regimeRelationships";

function buildHistory(): RegimeRelationshipSource[] {
  return [
    { date: "2026-03-05", realized_vol: 10, cor1m: 15 },
    { date: "2026-03-06", realized_vol: 12, cor1m: 18 },
    { date: "2026-03-07", realized_vol: 14, cor1m: 20 },
    { date: "2026-03-10", realized_vol: 16, cor1m: 17 },
  ];
}

describe("regimeRelationships", () => {
  it("merges live RVOL/COR1M values into the latest session before computing spread and z-scores", () => {
    const entries = buildRegimeRelationshipEntries(buildHistory(), {
      realized_vol: 15,
      cor1m: 21,
    });

    expect(entries).toHaveLength(4);
    expect(entries.at(-1)).toMatchObject({
      date: "2026-03-10",
      realizedVol: 15,
      cor1m: 21,
      spread: 6,
      quadrant: "Systemic Panic",
    });
    expect(entries.at(-1)?.zDivergence).toBeCloseTo(
      (entries.at(-1)?.cor1mZ ?? 0) - (entries.at(-1)?.realizedVolZ ?? 0),
      8,
    );
  });

  it("drops incomplete sessions so the relationship analytics only use comparable COR1M/RVOL pairs", () => {
    const entries = buildRegimeRelationshipEntries([
      ...buildHistory(),
      { date: "2026-03-11", realized_vol: null, cor1m: 25 },
      { date: "2026-03-12", realized_vol: 13, cor1m: undefined },
    ]);

    expect(entries).toHaveLength(4);
    expect(entries.every((entry) => Number.isFinite(entry.realizedVol) && Number.isFinite(entry.cor1m))).toBe(true);
  });

  it("summarizes spread regime, z-score bias, and latest quadrant from the computed series", () => {
    const entries = buildRegimeRelationshipEntries([
      { date: "2026-03-05", realized_vol: 16, cor1m: 13 },
      { date: "2026-03-06", realized_vol: 15, cor1m: 14 },
      { date: "2026-03-07", realized_vol: 14, cor1m: 15 },
      { date: "2026-03-10", realized_vol: 18, cor1m: 12 },
    ]);

    const summary = summarizeRegimeRelationship(entries);

    expect(summary).not.toBeNull();
    expect(summary).toMatchObject({
      spreadState: "Realized Lead",
      zScoreBias: "Realized Lead",
      latestQuadrant: "Stock Picker's Market",
    });
    expect(summary?.latestSpread).toBe(-6);
  });
});
