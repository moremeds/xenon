import type { FutuAccountEnv } from "@/lib/futuAccountEnv";
import { futuScopeQuery } from "@/lib/futuAccountEnv";
import type { OpenOrder } from "@/lib/types";
import type { FutuEnvCapabilities } from "@/lib/futuOrderTypes";

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

/** Contract § Working orders: cancel any Futu row with a broker id. */
export function futuManageableOpenOrder(order: OpenOrder): boolean {
  return isFutuOpenOrder(order) && Boolean(order.brokerOrderId);
}

/** Modify only rows whose order type is in `capabilities[env].modify`;
 *  no capabilities for the env → no modify. */
export function futuModifiableOpenOrder(
  order: OpenOrder,
  caps: FutuEnvCapabilities | null | undefined,
): boolean {
  return (
    futuManageableOpenOrder(order) &&
    (caps?.modify ?? []).some((t) => t === order.orderType)
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

/** Row terms recorded when a Futu cancel/modify outcome became uncertain. */
export type FutuManagementLock = {
  env: FutuAccountEnv;
  brokerOrderId: string;
  modifySequence: number | null;
  price: number | null;
  qty: number;
  auxPrice: number | null;
  trailValue: number | null;
  trailSpread: number | null;
};

/** The row terms an uncertain cancel/modify is pinned to. */
export function futuManagementLockFor(
  env: FutuAccountEnv,
  order: OpenOrder & { brokerOrderId: string },
): FutuManagementLock {
  return {
    env,
    brokerOrderId: order.brokerOrderId,
    modifySequence: order.modifySequence ?? null,
    price: order.limitPrice ?? null,
    qty: order.totalQuantity,
    auxPrice: order.auxPrice ?? null,
    trailValue: order.trailValue ?? null,
    trailSpread: order.trailSpread ?? null,
  };
}

const TERMINAL_ORDER_STATUS =
  /^(filled|filled_all|cancelled|cancelled_all|cancelled_part|apicancelled|inactive|rejected|failed|disabled|deleted)$/i;

/** True while `row` still shows the terms recorded in `lock` and is working.
 *  A terminal row or a changed modifySequence / price / qty / trigger /
 *  trail value / trail spread releases the lock. */
export function futuLockStillHolds(
  lock: FutuManagementLock,
  row: OpenOrder,
): boolean {
  if (row.brokerOrderId !== lock.brokerOrderId) return false;
  const statuses = [row.status, row.futuStatus];
  if (
    statuses.some((s) => typeof s === "string" && TERMINAL_ORDER_STATUS.test(s))
  ) {
    return false;
  }
  return (
    (row.modifySequence ?? null) === lock.modifySequence &&
    (row.limitPrice ?? null) === lock.price &&
    row.totalQuantity === lock.qty &&
    (row.auxPrice ?? null) === lock.auxPrice &&
    (row.trailValue ?? null) === lock.trailValue &&
    (row.trailSpread ?? null) === lock.trailSpread
  );
}

/** Shown in place of the IB ticket while the Futu account is active. */
export const FUTU_NO_IB_EXECUTION_COPY =
  "IB order entry is off for the Futu account. Use the position ticket on the Futu tab.";

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
