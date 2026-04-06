import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { join } from "path";
import { fileURLToPath } from "url";
import {
  needsCurrentEtSessionRetry,
  REGIME_STALE_RETRY_MS,
  REGIME_SYNC_CONFIG,
} from "../lib/useRegime";

const TEST_DIR = fileURLToPath(new URL(".", import.meta.url));
const HOOK_PATH = join(TEST_DIR, "../lib/useSyncHook.ts");
const hookSource = readFileSync(HOOK_PATH, "utf-8");

describe("Regime close-transition retry contract", () => {
  it("keeps retrying when the CRI payload still points to the prior ET session", () => {
    expect(
      needsCurrentEtSessionRetry(
        { date: "2026-03-11" },
        new Date("2026-03-12T20:15:00.000Z"),
      ),
    ).toBe(true);
  });

  it("stops retrying once the payload matches today's ET session", () => {
    expect(
      needsCurrentEtSessionRetry(
        { date: "2026-03-12" },
        new Date("2026-03-12T20:15:00.000Z"),
      ),
    ).toBe(false);
  });

  it("uses lightweight GET revalidation for stale close-transition payloads", () => {
    expect(REGIME_STALE_RETRY_MS).toBe(5000);
    expect(REGIME_SYNC_CONFIG.retryMethod).toBe("GET");
    expect(REGIME_SYNC_CONFIG.shouldRetry?.({
      date: "2026-03-11",
      scan_time: "2026-03-12T13:02:06.090911",
      market_open: false,
      vix: 24.23,
      vvix: 122.49,
      spy: 676.33,
      vix_5d_roc: 14.6,
      vvix_vix_ratio: 5.06,
      spx_100d_ma: 682.39,
      spx_distance_pct: -0.87,
      cor1m: 29.18,
      cor1m_previous_close: 28.87,
      cor1m_5d_change: 11.23,
      realized_vol: 11.51,
      cri: { score: 27, level: "ELEVATED", components: { vix: 8, vvix: 11, correlation: 5, momentum: 3 } },
      cta: { realized_vol: 11.51, exposure_pct: 86.9, forced_reduction_pct: 13.1, est_selling_bn: 52.4 },
      menthorq_cta: null,
      crash_trigger: {
        triggered: false,
        conditions: { spx_below_100d_ma: true, realized_vol_gt_25: false, cor1m_gt_60: false },
        values: {},
      },
      history: [],
      spy_closes: [],
    })).toBe(true);
  });

  it("wires retry scheduling into the shared sync hook", () => {
    expect(hookSource).toContain("shouldRetry?: (data: T) => boolean;");
    expect(hookSource).toContain("retryIntervalMs?: number;");
    expect(hookSource).toContain("retryMethod?: RetryMethod;");
    expect(hookSource).toContain("shouldRetry?.(json)");
    expect(hookSource).toContain("setTimeout(() => {");
  });
});
