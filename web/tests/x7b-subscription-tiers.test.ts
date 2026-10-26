import { describe, it, expect } from "vitest";
import {
  computeSubscriptionDiff,
  type DesiredSubscriptions,
} from "../lib/usePrices";
import { tagSubscriptionEntries } from "../lib/subscriptionTiers";
import {
  normalizeOptionContract,
  normalizeSymbolSubscriptions,
  uniqueOptionContracts,
  type OptionContract,
} from "../lib/pricesProtocol";

// X7b quote-tier tagging (contract-x7): portfolio + order legs subscribe as
// `held`, ticker-detail chain legs as `view`; anything else carries no tag
// (the relay defaults an absent tier to held). The wire format sends
// `symbols` entries as string | {symbol, tier} and `contracts` entries with
// an optional `tier`. A tier-only change must re-emit the entry so the relay
// recomputes the merged want tier.

const OPT: OptionContract = {
  symbol: "SPY",
  expiry: "20260116",
  strike: 580,
  right: "C",
};

const desired = (
  over: Partial<DesiredSubscriptions> = {},
): DesiredSubscriptions => ({
  symbols: [],
  contracts: [],
  indexes: [],
  forexes: [],
  stocksMeta: [],
  ...over,
});

describe("tagSubscriptionEntries", () => {
  it("tags held symbols as {symbol, tier:held} and leaves others untagged", () => {
    const { symbols } = tagSubscriptionEntries({
      heldSymbols: ["AAPL", "MSFT"],
      otherSymbols: ["QQQ"],
    });
    expect(symbols).toEqual([
      { symbol: "AAPL", tier: "held" },
      { symbol: "MSFT", tier: "held" },
      "QQQ",
    ]);
  });

  it("a symbol in both lists resolves to held (held > view)", () => {
    const { symbols } = tagSubscriptionEntries({
      heldSymbols: ["AAPL"],
      otherSymbols: ["AAPL", "QQQ"],
    });
    expect(symbols).toEqual([{ symbol: "AAPL", tier: "held" }, "QQQ"]);
  });

  it("tags held contracts held, chain contracts view, dedup keeps held", () => {
    const { contracts } = tagSubscriptionEntries({
      heldContracts: [OPT],
      viewContracts: [
        OPT, // same contract viewed on the chain — must stay held
        { symbol: "SPY", expiry: "20260116", strike: 600, right: "P" },
      ],
    });
    expect(contracts).toEqual([
      { ...OPT, tier: "held" },
      { symbol: "SPY", expiry: "20260116", strike: 600, right: "P", tier: "view" },
    ]);
  });

  it("normalizes case/whitespace on tagged symbols", () => {
    const { symbols } = tagSubscriptionEntries({ heldSymbols: [" aapl "] });
    expect(symbols).toEqual([{ symbol: "AAPL", tier: "held" }]);
  });
});

describe("tier merge — held upgrades a kept view entry", () => {
  it("normalizeSymbolSubscriptions: view then bare → bare (held)", () => {
    expect(
      normalizeSymbolSubscriptions([
        { symbol: "AAPL", tier: "view" },
        "AAPL",
      ]),
    ).toEqual(["AAPL"]);
  });

  it("uniqueOptionContracts: view then held → held", () => {
    expect(
      uniqueOptionContracts([
        { ...OPT, tier: "view" },
        { ...OPT, tier: "held" },
      ]),
    ).toEqual([{ ...OPT, tier: "held" }]);
  });
});

describe("normalizeOptionContract — tier passthrough", () => {
  it("keeps an optional tier on the normalized contract", () => {
    expect(
      normalizeOptionContract({
        symbol: "spy",
        expiry: "2026-01-16",
        strike: 580,
        right: "C",
        tier: "view",
      }),
    ).toEqual({ ...OPT, tier: "view" });
  });
});

describe("computeSubscriptionDiff — tiered symbols", () => {
  it("emits wire entries {symbol, tier} for tagged, strings for untagged", () => {
    const diff = computeSubscriptionDiff(
      "",
      desired({
        symbols: [{ symbol: "AAPL", tier: "held" }, "QQQ"],
      }),
    );
    expect(diff.changed).toBe(true);
    expect(diff.addedSymbols).toEqual([
      { symbol: "AAPL", tier: "held" },
      "QQQ",
    ]);
  });

  it("is stable across an unchanged recompute (no spurious resend)", () => {
    const d = desired({
      symbols: [{ symbol: "AAPL", tier: "held" }, "QQQ"],
      contracts: [{ ...OPT, tier: "view" }],
    });
    const first = computeSubscriptionDiff("", d);
    const again = computeSubscriptionDiff(first.currentHash, d);
    expect(again.changed).toBe(false);
  });

  it("a tier-only change re-emits the symbol without removing it", () => {
    const first = computeSubscriptionDiff(
      "",
      desired({ symbols: [{ symbol: "AAPL", tier: "held" }] }),
    );
    const diff = computeSubscriptionDiff(
      first.currentHash,
      desired({ symbols: [{ symbol: "AAPL", tier: "view" }] }),
    );
    expect(diff.changed).toBe(true);
    expect(diff.addedSymbols).toEqual([{ symbol: "AAPL", tier: "view" }]);
    expect(diff.removedSymbols).toEqual([]);
  });

  it("removed symbols still come out as bare symbols", () => {
    const first = computeSubscriptionDiff(
      "",
      desired({ symbols: [{ symbol: "AAPL", tier: "held" }] }),
    );
    const diff = computeSubscriptionDiff(first.currentHash, desired());
    expect(diff.removedSymbols).toEqual(["AAPL"]);
  });
});

describe("computeSubscriptionDiff — tiered contracts", () => {
  it("emits the tiered contract and re-emits on a tier-only change", () => {
    const first = computeSubscriptionDiff(
      "",
      desired({ contracts: [{ ...OPT, tier: "view" }] }),
    );
    expect(first.addedContracts).toEqual([{ ...OPT, tier: "view" }]);

    const diff = computeSubscriptionDiff(
      first.currentHash,
      desired({ contracts: [{ ...OPT, tier: "held" }] }),
    );
    expect(diff.changed).toBe(true);
    expect(diff.addedContracts).toEqual([{ ...OPT, tier: "held" }]);
    expect(diff.removedContractKeys).toEqual([]);
  });

  it("removed contracts still come out as bare option keys", () => {
    const first = computeSubscriptionDiff(
      "",
      desired({ contracts: [{ ...OPT, tier: "view" }] }),
    );
    const diff = computeSubscriptionDiff(first.currentHash, desired());
    expect(diff.removedContractKeys).toEqual(["SPY_20260116_580_C"]);
  });
});
