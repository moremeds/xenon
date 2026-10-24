import type { FutuAccountEnv } from "@/lib/futuAccountEnv";
import type { OpenOrder } from "@/lib/types";
import { isFutuOpenOrder } from "@/lib/futuOpenOrder";

export type OrderActionScope = {
  broker: "IB" | "FUTU";
  accountEnv?: FutuAccountEnv;
};

export type TicketAnchor = OrderActionScope;

export function resolveFutuEnv(
  order: OpenOrder,
  fallback: FutuAccountEnv,
): FutuAccountEnv {
  if (order.accountEnv === "paper" || order.accountEnv === "live") {
    return order.accountEnv;
  }
  return fallback;
}

export function orderActionScope(
  order: OpenOrder,
  fallbackFutuEnv: FutuAccountEnv,
): OrderActionScope {
  if (isFutuOpenOrder(order)) {
    return {
      broker: "FUTU",
      accountEnv: resolveFutuEnv(order, fallbackFutuEnv),
    };
  }
  return { broker: "IB" };
}

export function ordersScopeKey(
  scope: OrderActionScope | OpenOrder,
  fallbackFutuEnv: FutuAccountEnv = "paper",
): string {
  const normalized =
    "broker" in scope && (scope.broker === "IB" || scope.broker === "FUTU")
      ? scope
      : orderActionScope(scope as OpenOrder, fallbackFutuEnv);
  if (normalized.broker === "FUTU") {
    return `FUTU:${normalized.accountEnv ?? fallbackFutuEnv}`;
  }
  return "IB";
}

export function scopesMatch(a: OrderActionScope, b: OrderActionScope): boolean {
  if (a.broker !== b.broker) return false;
  if (a.broker === "FUTU") {
    return a.accountEnv === b.accountEnv;
  }
  return true;
}

export function ticketAnchorFromTable(
  futuOrderMode: boolean,
  futuAccountEnv: FutuAccountEnv,
): TicketAnchor {
  return futuOrderMode
    ? { broker: "FUTU", accountEnv: futuAccountEnv }
    : { broker: "IB" };
}
