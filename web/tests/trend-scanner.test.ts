import { describe, it, expect } from "vitest";
import type { TrendCandidate, ScannerData } from "@/lib/types";

function makeMockCandidate(
  overrides: Partial<TrendCandidate> = {},
): TrendCandidate {
  return {
    ticker: "NVDA",
    snapshot_timestamp: "2026-04-10T08:45:12-04:00",
    spot_price: 148.3,
    direction: "bullish",
    final_score: 0.82,
    scores: { trend: 0.91, structure: 0.75, volatility: 0.68, flow: 0.85 },
    indicators: {
      ma_20: 142.5,
      ma_50: 138.2,
      ma_200: 125.8,
      rsi: 62.3,
      adx: 32.1,
      macd_histogram: 1.45,
      bbw: 0.08,
      rs_vs_spy: 1.15,
      iv_rank: 22,
      gamma_flip: 145,
      call_wall: 160,
      put_wall: 140,
    },
    summaries: {
      trend: "Full MA stack, ADX 32",
      structure: "Above gamma flip",
      vol: "IV rank 22, normal",
      flow: "4 ask-side prints",
    },
    structure_hint: "long_call",
    catalysts: [],
    invalidation: 142.5,
    flags: ["four_gates_not_applied"],
    holding_window: "5-15 trading days",
    ...overrides,
  };
}

describe("TrendCandidate type shape", () => {
  it("has all required fields", () => {
    const c = makeMockCandidate();
    expect(c.ticker).toBe("NVDA");
    expect(c.scores.trend).toBe(0.91);
    expect(c.indicators.rsi).toBe(62.3);
    expect(c.summaries.flow).toBe("4 ask-side prints");
  });

  it("supports bearish direction", () => {
    const c = makeMockCandidate({ direction: "bearish", ticker: "SPY" });
    expect(c.direction).toBe("bearish");
  });

  it("supports flags array", () => {
    const c = makeMockCandidate({ flags: ["event_premium", "breakout"] });
    expect(c.flags).toHaveLength(2);
  });
});

describe("ScannerData shape", () => {
  it("has funnel metadata", () => {
    const data: ScannerData = {
      scan_id: "trend_20260410_0845",
      scan_timestamp: "2026-04-10T08:45:12-04:00",
      market_context: { spy_close: 523.45, vix_close: 18.2, regime: "bullish" },
      universe_size: 743,
      stage_a_survivors: 187,
      stage_b_survivors: 92,
      candidates: [makeMockCandidate()],
    };
    expect(data.universe_size).toBe(743);
    expect(data.candidates).toHaveLength(1);
  });
});
