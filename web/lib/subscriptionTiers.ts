import {
  normalizeSymbolSubscriptions,
  uniqueOptionContracts,
  type OptionContract,
  type SymbolSubscription,
} from "./pricesProtocol";

/**
 * X7b quote-tier tagging (contract-x7): portfolio + order legs subscribe as
 * `held`, ticker-detail chain legs as `view`. Everything else stays untagged
 * — the relay defaults an absent tier to held. When the same contract is both
 * held and viewed, held wins (held > view merge, same as the relay).
 */
export function tagSubscriptionEntries(input: {
  heldSymbols?: string[];
  otherSymbols?: string[];
  heldContracts?: OptionContract[];
  viewContracts?: OptionContract[];
}): { symbols: SymbolSubscription[]; contracts: OptionContract[] } {
  const symbols = normalizeSymbolSubscriptions([
    ...(input.heldSymbols ?? []).map((symbol) => ({
      symbol,
      tier: "held" as const,
    })),
    ...(input.otherSymbols ?? []),
  ]);
  const contracts = uniqueOptionContracts([
    ...(input.heldContracts ?? []).map((c) => ({ ...c, tier: "held" as const })),
    ...(input.viewContracts ?? []).map((c) => ({ ...c, tier: "view" as const })),
  ]);
  return { symbols, contracts };
}
