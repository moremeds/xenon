import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * Tests for the new sync-based hooks and API routes.
 *
 * - useSyncHook: factory behavior (GET/POST flow, interval, cleanup)
 * - Flow Analysis API: GET cache read, POST spawn
 * - Scanner API: GET cache read, POST spawn
 */

// ---------------------------------------------------------------------------
// Mock fs/promises and child_process for API route tests
// ---------------------------------------------------------------------------

const mockReadFile = vi.fn();
const mockWriteFile = vi.fn();
vi.mock("fs/promises", () => ({
  readFile: (...args: unknown[]) => mockReadFile(...args),
  writeFile: (...args: unknown[]) => mockWriteFile(...args),
}));

const mockSpawn = vi.fn();
vi.mock("child_process", () => ({
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));

// Mock xenonFetch — flow-analysis route now proxies via xenonFetch
const mockXenonFetch = vi.fn();
vi.mock("@/lib/xenonApi", () => ({
  xenonFetch: (...args: unknown[]) => mockXenonFetch(...args),
  XenonApiError: class extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail: string) {
      super(`Xenon API ${status}: ${detail}`);
      this.name = "XenonApiError";
      this.status = status;
      this.detail = detail;
    }
  },
}));

// ---------------------------------------------------------------------------
// Flow Analysis API — GET (proxies to FastAPI via xenonFetch)
// ---------------------------------------------------------------------------

describe("Flow Analysis API GET", () => {
  beforeEach(() => {
    vi.resetModules();
    mockXenonFetch.mockReset();
  });

  it("returns cached data when file exists", async () => {
    const cached = {
      analysis_time: "2026-03-06T12:00:00",
      positions_scanned: 5,
      supports: [{ ticker: "AAPL", position: "Long Calls", strength: 30 }],
      against: [],
      watch: [],
      neutral: [],
    };
    mockXenonFetch.mockResolvedValueOnce(cached);

    const { GET } = await import("../app/api/flow-analysis/route");
    const res = await GET(new Request("http://localhost/api/flow-analysis"));
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.positions_scanned).toBe(5);
    expect(body.supports).toHaveLength(1);
  });

  it("returns empty structure when cache file is missing", async () => {
    mockXenonFetch.mockRejectedValueOnce(new Error("ECONNREFUSED"));

    const { GET } = await import("../app/api/flow-analysis/route");
    const res = await GET(new Request("http://localhost/api/flow-analysis"));
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.positions_scanned).toBe(0);
    expect(body.supports).toEqual([]);
    expect(body.against).toEqual([]);
    expect(body.neutral).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Scanner API — GET tests removed: /api/scanner now proxies to FastAPI via
// xenonFetch and no longer reads data/scan_results.json from disk. The
// fs/promises::readFile cache contract these tests asserted is gone.
// FastAPI scan-result coverage lives in scripts/tests/test_blotter_query.py
// and the Python scan-result query tests.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Type shape tests — ensure types are properly exported
// ---------------------------------------------------------------------------

describe("Type exports", () => {
  it("FlowAnalysisData has required fields", async () => {
    const data: import("@/lib/types").FlowAnalysisData = {
      analysis_time: "2026-03-06",
      positions_scanned: 0,
      supports: [],
      against: [],
      watch: [],
      neutral: [],
    };
    expect(data.analysis_time).toBe("2026-03-06");
    expect(data.supports).toEqual([]);
  });

  it("ScannerData has required fields", async () => {
    const data: import("@/lib/types").ScannerData = {
      scan_id: "trend_20260306",
      scan_timestamp: "2026-03-06",
      market_context: { spy_close: 520, vix_close: 17, regime: "bullish" },
      universe_size: 0,
      stage_a_survivors: 0,
      stage_b_survivors: 0,
      candidates: [],
    };
    expect(data.scan_timestamp).toBe("2026-03-06");
    expect(data.candidates).toEqual([]);
  });

  it("FlowAnalysisPosition has all fields", async () => {
    const pos: import("@/lib/types").FlowAnalysisPosition = {
      ticker: "AAPL",
      position: "Long Calls",
      direction: "LONG",
      flow_direction: "ACCUMULATION",
      flow_label: "65% ACCUM",
      flow_class: "accum",
      strength: 30,
      buy_ratio: 0.65,
      note: "Strong accumulation",
    };
    expect(pos.ticker).toBe("AAPL");
    expect(pos.flow_class).toBe("accum");
  });

  it("TrendCandidate has all fields", async () => {
    const c: import("@/lib/types").TrendCandidate = {
      ticker: "NVDA",
      snapshot_timestamp: "2026-03-06T12:00:00",
      spot_price: 148.3,
      direction: "bullish",
      final_score: 0.82,
      scores: { trend: 0.91, structure: 0.75, volatility: 0.68, flow: 0.85 },
      indicators: {
        ma_20: 142.5,
        ma_50: 138,
        ma_200: 125,
        rsi: 62,
        adx: 32,
        macd_histogram: 1.45,
        bbw: 0.08,
        rs_vs_spy: 1.15,
        iv_rank: 22,
        gamma_flip: 145,
        call_wall: 160,
        put_wall: 140,
      },
      summaries: {
        trend: "Full MA stack",
        structure: "Above gamma flip",
        vol: "IV rank 22",
        flow: "4 ask-side prints",
      },
      structure_hint: "long_call",
      catalysts: [],
      invalidation: 142.5,
      flags: ["four_gates_not_applied"],
      holding_window: "5-15 trading days",
    };
    expect(c.ticker).toBe("NVDA");
    expect(c.scores.trend).toBe(0.91);
  });
});
