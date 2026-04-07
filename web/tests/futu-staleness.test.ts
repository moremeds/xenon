import { describe, it, expect } from "vitest";
import { computeFutuStaleness } from "@/lib/futuStaleness";
import type { FutuPortfolioEnvelope } from "@/lib/futuPortfolioAdapter";

const fixedNow = Date.parse("2026-04-07T12:00:00.000Z");

function env(overrides: Partial<FutuPortfolioEnvelope> = {}): FutuPortfolioEnvelope {
  return {
    fetched_at: "2026-04-07T11:59:30.000Z", // 30s ago at fixedNow
    data_as_of: "2026-04-07T11:59:30.000Z",
    account_id: "12345",
    source: "futu",
    is_stale: false,
    warnings: [],
    positions: [],
    count: 0,
    account_summary: {
      net_liquidation: 0,
      equity_with_loan: 0,
      cash: 0,
      settled_cash: 0,
      buying_power: 0,
      available_funds: 0,
      initial_margin: 0,
      maintenance_margin: 0,
      excess_liquidity: 0,
      gross_position_value: 0,
      unrealized_pnl: 0,
      daily_pnl: 0,
      realized_pnl: 0,
      dividends: null,
      previous_day_ewl: null,
      reg_t_equity: null,
      sma: null,
    },
    ...overrides,
  };
}

describe("computeFutuStaleness", () => {
  it("returns never_synced when flag is true", () => {
    expect(
      computeFutuStaleness({
        envelope: env(),
        error: null,
        neverSynced: true,
        marketOpen: true,
        now: fixedNow,
      }),
    ).toBe("never_synced");
  });

  it("returns down when no envelope AND error present", () => {
    expect(
      computeFutuStaleness({
        envelope: null,
        error: "FastAPI unreachable",
        neverSynced: false,
        marketOpen: true,
        now: fixedNow,
      }),
    ).toBe("down");
  });

  it("returns never_synced when no envelope AND no error", () => {
    // First-render state before any fetch has resolved.
    expect(
      computeFutuStaleness({
        envelope: null,
        error: null,
        neverSynced: false,
        marketOpen: true,
        now: fixedNow,
      }),
    ).toBe("never_synced");
  });

  it("returns stale when envelope.is_stale is true (Next.js disk fallback)", () => {
    // Per Codex #5: is_stale from the proxy fallback = degraded-but-usable,
    // NOT down. Even if fetched_at is fresh, is_stale wins.
    expect(
      computeFutuStaleness({
        envelope: env({ is_stale: true }),
        error: null,
        neverSynced: false,
        marketOpen: true,
        now: fixedNow,
      }),
    ).toBe("stale");
  });

  it("returns stale when market open AND fetched_at is older than 60s", () => {
    // 90s ago
    const old = new Date(fixedNow - 90_000).toISOString();
    expect(
      computeFutuStaleness({
        envelope: env({ fetched_at: old, data_as_of: old }),
        error: null,
        neverSynced: false,
        marketOpen: true,
        now: fixedNow,
      }),
    ).toBe("stale");
  });

  it("returns live when market open AND fetched_at is fresh", () => {
    expect(
      computeFutuStaleness({
        envelope: env(),
        error: null,
        neverSynced: false,
        marketOpen: true,
        now: fixedNow,
      }),
    ).toBe("live");
  });

  it("returns live when market closed even if fetched_at is old", () => {
    // Market closed: stale threshold does not apply.
    const old = new Date(fixedNow - 86_400_000).toISOString(); // 24h ago
    expect(
      computeFutuStaleness({
        envelope: env({ fetched_at: old, data_as_of: old }),
        error: null,
        neverSynced: false,
        marketOpen: false,
        now: fixedNow,
      }),
    ).toBe("live");
  });

  it("is_stale wins over the fresh fetched_at check", () => {
    // Envelope claims fresh timestamp but is_stale flag set (e.g. disk
    // fallback ran against a prior fresh write). Precedence: stale.
    expect(
      computeFutuStaleness({
        envelope: env({ is_stale: true, fetched_at: new Date(fixedNow).toISOString() }),
        error: null,
        neverSynced: false,
        marketOpen: true,
        now: fixedNow,
      }),
    ).toBe("stale");
  });

  it("never_synced flag wins over error", () => {
    expect(
      computeFutuStaleness({
        envelope: null,
        error: "some error",
        neverSynced: true,
        marketOpen: true,
        now: fixedNow,
      }),
    ).toBe("never_synced");
  });
});
