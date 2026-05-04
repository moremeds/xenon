import type { PositionRule, ProtectionState } from "@/lib/api/positionRules";
import type { PortfolioLeg, PortfolioPosition } from "@/lib/types";

const STATE_URGENCY: Record<ProtectionState, number> = {
  FAILED: 6,
  TRIGGERED: 5,
  PENDING_ARM: 4,
  ARMED: 3,
  CANCELED: 2,
  CLOSED: 1,
  SUPERSEDED: 1,
};

function normalizeExpiry(expiry: unknown): string | null {
  if (typeof expiry !== "string" || !expiry || expiry === "N/A") return null;
  return expiry.replaceAll("-", "");
}

function formatStrike(strike: unknown): string | null {
  if (typeof strike !== "number" || !Number.isFinite(strike)) return null;
  if (Number.isInteger(strike)) return String(strike);
  return String(strike).replace(/0+$/, "").replace(/\.$/, "");
}

function legRight(leg: PortfolioLeg): "C" | "P" | null {
  if (leg.type === "Call") return "C";
  if (leg.type === "Put") return "P";
  return null;
}

function legAction(leg: PortfolioLeg): "BUY" | "SELL" {
  return leg.direction === "SHORT" ? "SELL" : "BUY";
}

function simpleKeysForPosition(pos: PortfolioPosition): Set<string> {
  const keys = new Set<string>();
  const ticker = pos.ticker.toUpperCase();

  if (pos.structure_type === "Stock" || pos.legs.every((leg) => leg.type === "Stock")) {
    keys.add(`STK::${ticker}`);
  }

  if (pos.legs.length === 1) {
    const leg = pos.legs[0];
    const right = legRight(leg);
    const expiry = normalizeExpiry(pos.expiry);
    const strike = formatStrike(leg.strike);
    if (right && expiry && strike) {
      keys.add(`OPT::${ticker}::${expiry}::${strike}::${right}`);
    }
  }

  const shortOption = pos.legs.find((leg) => leg.direction === "SHORT" && legRight(leg));
  const longOption = pos.legs.find((leg) => leg.direction === "LONG" && legRight(leg));
  const spreadExpiry = normalizeExpiry(pos.expiry);
  if (shortOption && longOption && spreadExpiry && legRight(shortOption) === legRight(longOption)) {
    const shortStrike = formatStrike(shortOption.strike);
    const longStrike = formatStrike(longOption.strike);
    if (shortStrike && longStrike) {
      keys.add(`CS::${ticker}::${spreadExpiry}::${shortStrike}::${longStrike}::${legRight(shortOption)}`);
    }
  }

  if (pos.legs.some((leg) => leg.type === "Stock") && shortOption && legRight(shortOption) === "C") {
    const expiry = normalizeExpiry(pos.expiry);
    const strike = formatStrike(shortOption.strike);
    if (expiry && strike) {
      keys.add(`CC::${ticker}::${expiry}::${strike}`);
    }
  }

  return keys;
}

function descriptorLegsMatchPosition(rule: PositionRule, pos: PortfolioPosition): boolean {
  const descriptorLegs = Array.isArray(rule.position_descriptor.legs)
    ? rule.position_descriptor.legs
    : [];
  if (descriptorLegs.length === 0) return false;

  return descriptorLegs.every((raw) => {
    if (!raw || typeof raw !== "object") return false;
    const descriptor = raw as Record<string, unknown>;
    const symbol = String(descriptor.symbol ?? "").toUpperCase();
    if (symbol !== pos.ticker.toUpperCase()) return false;

    const secType = String(descriptor.sec_type ?? descriptor.secType ?? "");
    if (secType === "STK") {
      return pos.legs.some((leg) => leg.type === "Stock");
    }

    const expiry = normalizeExpiry(descriptor.expiry);
    const strike = formatStrike(descriptor.strike);
    const right = descriptor.right;
    const action = descriptor.action;
    return pos.legs.some(
      (leg) =>
        legRight(leg) === right &&
        normalizeExpiry(pos.expiry) === expiry &&
        formatStrike(leg.strike) === strike &&
        legAction(leg) === action,
    );
  });
}

export function rulesForPortfolioPosition(
  pos: PortfolioPosition,
  rules: readonly PositionRule[],
): PositionRule[] {
  const keys = simpleKeysForPosition(pos);
  return rules.filter((rule) => keys.has(rule.position_key) || descriptorLegsMatchPosition(rule, pos));
}

export function dominantProtectionState(rules: readonly PositionRule[]): ProtectionState {
  return rules.reduce<ProtectionState>(
    (best, rule) => (STATE_URGENCY[rule.state] > STATE_URGENCY[best] ? rule.state : best),
    "SUPERSEDED",
  );
}
