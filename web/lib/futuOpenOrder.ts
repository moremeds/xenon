import type { FutuAccountEnv } from "@/lib/futuAccountEnv";
import { futuScopeQuery } from "@/lib/futuAccountEnv";
import type { OpenOrder } from "@/lib/types";

export function isFutuOpenOrder(order: OpenOrder): boolean {
  return order.broker === "FUTU" || Boolean(order.brokerOrderId);
}

/** Stable key for pending cancel/modify maps (Futu permId is always 0). */
export function openOrderActionKey(order: OpenOrder): string {
  if (order.brokerOrderId) return `futu:${order.brokerOrderId}`;
  if (order.permId > 0) return `ib:perm:${order.permId}`;
  if (order.orderId > 0) return `ib:oid:${order.orderId}`;
  return `ib:unknown:${order.symbol}`;
}

export function futuOrdersApiPath(env: FutuAccountEnv): string {
  return `/api/orders${futuScopeQuery(env)}`;
}

export function futuCancelApiPath(env: FutuAccountEnv): string {
  return `/api/orders/cancel${futuScopeQuery(env)}`;
}

export function futuModifyApiPath(env: FutuAccountEnv): string {
  return `/api/orders/modify${futuScopeQuery(env)}`;
}

export function futuPlaceApiPath(env: FutuAccountEnv): string {
  return `/api/orders/place${futuScopeQuery(env)}`;
}

/** Contract: cancel/modify only LMT rows with a Futu broker id. */
export function futuManageableOpenOrder(order: OpenOrder): boolean {
  return (
    isFutuOpenOrder(order) &&
    order.orderType === "LMT" &&
    Boolean(order.brokerOrderId)
  );
}

export function orderStillOpen(
  data: { open_orders: OpenOrder[] },
  order: OpenOrder,
): boolean {
  if (isFutuOpenOrder(order) && order.brokerOrderId) {
    return data.open_orders.some(
      (o) => o.brokerOrderId === order.brokerOrderId,
    );
  }
  const permId = order.permId;
  const orderId = order.orderId;
  return data.open_orders.some(
    (o) => o.permId === permId || (o.orderId === orderId && orderId !== 0),
  );
}

export function findOpenOrderInSnapshot(
  data: { open_orders: OpenOrder[] },
  order: OpenOrder,
): OpenOrder | undefined {
  if (isFutuOpenOrder(order) && order.brokerOrderId) {
    return data.open_orders.find(
      (o) => o.brokerOrderId === order.brokerOrderId,
    );
  }
  const permId = order.permId;
  const orderId = order.orderId;
  return data.open_orders.find(
    (o) => o.permId === permId || (o.orderId === orderId && orderId !== 0),
  );
}

/** Shown when Futu accepted a modify but `confirmed` is false on the response. */
export const FUTU_MODIFY_SENT_UNCONFIRMED_COPY =
  "sent, Futu has not reported the new terms yet";

/** Uncertain cancel/modify — hide row actions until reconciliation or reload. */
export const FUTU_MANAGEMENT_UNCONFIRMED_COPY =
  "unconfirmed — check the Futu app";

export function futuManagementLockKey(
  env: FutuAccountEnv,
  brokerOrderId: string,
): string {
  return `${env}:${brokerOrderId}`;
}

export function computeNextModifySequence(
  order: OpenOrder,
  actionKey: string,
  localSeqByKey: Map<string, number>,
): number {
  const rowSeq = order.modifySequence ?? 0;
  const localSeq = localSeqByKey.get(actionKey) ?? 0;
  return Math.max(rowSeq, localSeq) + 1;
}

export function recordAppliedModifySequence(
  actionKey: string,
  localSeqByKey: Map<string, number>,
  applied: number | undefined,
  sentSequence: number,
): void {
  const next = Math.max(
    localSeqByKey.get(actionKey) ?? 0,
    typeof applied === "number" ? applied : sentSequence,
  );
  localSeqByKey.set(actionKey, next);
}

/** Last 4 of broker account for ticket badge (never full number). */
export function futuAccountTail(
  accountId: string | null | undefined,
): string | null {
  if (!accountId) return null;
  const digits = accountId.replace(/\D/g, "");
  if (digits.length >= 4) return digits.slice(-4);
  if (accountId.length >= 4) return accountId.slice(-4);
  return accountId;
}
