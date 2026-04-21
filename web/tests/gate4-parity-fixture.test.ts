import { describe, expect, test } from "vitest";
import fixture from "../../scripts/tests/fixtures/gate4_parity.json";
import { checkNakedShortRisk } from "../lib/nakedShortGuard";
import type { NakedShortPortfolio, OrderPayload } from "../lib/nakedShortGuard";

type FixtureCase = {
  name: string;
  request: {
    type: "stock" | "option" | "combo";
    symbol: string;
    action: "BUY" | "SELL";
    quantity: number;
    right: "C" | "P" | null;
    expiry: string | null;
    strike: number | null;
    multiplier: number;
    limitPrice: number;
  };
  portfolio: NakedShortPortfolio;
  expected: {
    accept: boolean;
    reason_code: string | null;
  };
};

function toOrderPayload(r: FixtureCase["request"]): OrderPayload {
  return {
    type: r.type,
    symbol: r.symbol,
    action: r.action,
    quantity: r.quantity,
    right: r.right ?? undefined,
    expiry: r.expiry ?? undefined,
    strike: r.strike ?? undefined,
    limitPrice: r.limitPrice,
  } as OrderPayload;
}

describe("Gate 4 parity fixture — TS guard matches Python preflight", () => {
  for (const c of (fixture as { cases: FixtureCase[] }).cases) {
    // The TS guard does not distinguish UNIVERSE_UNKNOWN / INDEX_HAS_NO_STOCK —
    // those are server-only gates. Skip those two reason codes in the TS runner;
    // parity for Gate 4 coverage cases (INDEX_CALL_UNCOVERED, ETF_CALL_UNCOVERED,
    // INSUFFICIENT_SHARES) is the point of this test.
    if (
      c.expected.reason_code === "UNIVERSE_UNKNOWN" ||
      c.expected.reason_code === "INDEX_HAS_NO_STOCK"
    ) {
      continue;
    }
    test(c.name, () => {
      const result = checkNakedShortRisk(
        toOrderPayload(c.request),
        c.portfolio,
      );
      expect(result.allowed).toBe(c.expected.accept);
    });
  }
});
