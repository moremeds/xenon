import { describe, expect, it } from "vitest";

import {
  dominantProtectionState,
  rulesForPortfolioPosition,
} from "@/lib/positionRulesUi";
import type { PositionRule } from "@/lib/api/positionRules";
import type { PortfolioPosition } from "@/lib/types";

function stockPosition(ticker = "AAPL"): PortfolioPosition {
  return {
    id: 1,
    ticker,
    structure: "Long Stock",
    structure_type: "Stock",
    risk_profile: "equity",
    expiry: "N/A",
    contracts: 100,
    direction: "LONG",
    entry_cost: 10000,
    max_risk: null,
    market_value: 10100,
    legs: [
      {
        direction: "LONG",
        contracts: 100,
        type: "Stock",
        strike: null,
        entry_cost: 10000,
        avg_cost: 100,
        market_price: 101,
        market_value: 10100,
      },
    ],
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "2026-05-04",
  };
}

function optionPosition(): PortfolioPosition {
  return {
    ...stockPosition("GOOG"),
    structure: "Long Call $315",
    structure_type: "Option",
    risk_profile: "defined",
    expiry: "2026-04-17",
    contracts: 1,
    direction: "LONG",
    legs: [
      {
        direction: "LONG",
        contracts: 1,
        type: "Call",
        strike: 315,
        entry_cost: 500,
        avg_cost: 5,
        market_price: 6,
        market_value: 600,
      },
    ],
  };
}

function rule(overrides: Partial<PositionRule> = {}): PositionRule {
  return {
    protection_id: 10,
    position_key: "STK::AAPL",
    rule_kind: "stop_loss",
    state: "ARMED",
    asset_class: "stock",
    config: {},
    state_data: {},
    position_descriptor: {
      legs: [{ sec_type: "STK", symbol: "AAPL", action: "BUY", ratio: 1 }],
    },
    native_order_perm_id: null,
    armed_at: null,
    triggered_at: null,
    ...overrides,
  };
}

describe("position rules UI helpers", () => {
  it("matches a stock rule to the portfolio stock row", () => {
    expect(rulesForPortfolioPosition(stockPosition(), [rule()])).toHaveLength(1);
  });

  it("matches long options across dashed and compact expiries", () => {
    const matching = rule({
      position_key: "OPT::GOOG::20260417::315::C",
      asset_class: "long_option",
      position_descriptor: {
        legs: [
          {
            sec_type: "OPT",
            symbol: "GOOG",
            expiry: "20260417",
            strike: 315,
            right: "C",
            action: "BUY",
            ratio: 1,
          },
        ],
      },
    });

    expect(rulesForPortfolioPosition(optionPosition(), [matching])).toHaveLength(1);
  });

  it("chooses the most urgent rule state for the badge", () => {
    expect(
      dominantProtectionState([
        rule({ state: "ARMED" }),
        rule({ state: "PENDING_ARM" }),
        rule({ state: "FAILED" }),
      ]),
    ).toBe("FAILED");
  });
});
