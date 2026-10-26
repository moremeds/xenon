"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { OpenOrder, OrdersData } from "@/lib/types";
import type { ModifyOrderRequest } from "@/lib/orderModify";
import { getReasonToast } from "@/lib/orderReasonCodes";
import { readReasonCode } from "@/lib/readReasonCode";
import {
  isUncertainPlaceResponse,
  REPLACEMENT_UNCERTAIN_COPY,
} from "@/lib/order/submitPlace";
import { randomAttemptId } from "@/components/ticker-detail/useClientAttemptId";
import { type FutuAccountEnv, useFutuAccountEnv } from "@/lib/futuAccountEnv";
import {
  computeNextModifySequence,
  findOpenOrderInSnapshot,
  FUTU_MANAGEMENT_UNCONFIRMED_COPY,
  FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
  futuCancelApiPath,
  futuLockStillHolds,
  futuManagementLockFor,
  futuManagementLockKey,
  futuModifyApiPath,
  futuOrdersApiPath,
  isFutuOpenOrder,
  openOrderActionKey,
  orderStillOpen,
  recordAppliedModifySequence,
  type FutuManagementLock,
} from "@/lib/futuOpenOrder";
import { ordersScopeKey, resolveFutuEnv } from "@/lib/orderActionScope";
import { isUncertainFutuManagementResponse } from "@/lib/futuManagementUncertain";
import { futuModifyTermsText } from "@/lib/futuOrderTypes";

/** Result surfaced to callers of requestCancel/requestModify so the row can
 *  render a FAILED pill (F6.2). `status` is the HTTP status from the Next.js
 *  route — 503/409 are the "server said no, do not mark cancelled" cases. */
export type ActionResult =
  | { ok: true }
  | { ok: false; status: number; reasonCode: string | null; message: string };

/** Snapshot of a cancelled order for the executed table */
export type CancelledOrder = {
  permId: number;
  symbol: string;
  action: string;
  orderType: string;
  totalQuantity: number;
  limitPrice: number | null;
  cancelledAt: string; // ISO timestamp
};

export type PendingModify = {
  order: OpenOrder;
  newPrice?: number;
  newQuantity?: number;
};

type ModifyRequestWithOutsideRth = ModifyOrderRequest & {
  outsideRth?: boolean;
};

type Notification = {
  type: "error" | "warning" | "success";
  message: string;
  duration?: number;
};

type OrderActionsContextValue = {
  pendingCancels: Map<string, OpenOrder>;
  pendingModifies: Map<string, PendingModify>;
  cancelledOrders: CancelledOrder[];
  isPendingCancel: (order: OpenOrder) => boolean;
  isPendingModify: (order: OpenOrder) => boolean;
  requestCancel: (order: OpenOrder) => Promise<ActionResult>;
  requestModify: (
    order: OpenOrder,
    request: ModifyOrderRequest,
  ) => Promise<ActionResult>;
  drainNotifications: () => Notification[];
  setOrdersUpdater: (
    fn: ((data: OrdersData) => void) | null,
    scopeKey?: string | null,
  ) => void;
  isFutuManagementLocked: (order: OpenOrder) => boolean;
  /** Same-scope orders snapshot (from useOrders) — reconciles Futu locks. */
  observeOrdersSnapshot: (scopeKey: string, data: OrdersData) => void;
  /** permIds whose combo replacement ended uncertain: the replacement form
   * for that order is locked (no retry) until the page reloads. */
  lockedReplacements: Set<number>;
};

const OrderActionsContext = createContext<OrderActionsContextValue | null>(
  null,
);

const POLL_INTERVAL_MS = 5_000;

function errorMessageFromBody(
  json: Record<string, unknown>,
  reasonCode: string | null,
  fallback: string,
): string {
  if (reasonCode) return getReasonToast(reasonCode).copy;
  if (typeof json?.error === "string") return json.error;
  if (typeof json?.detail === "string") return json.detail;
  const detailMessage = (json?.detail as { message?: string } | undefined)
    ?.message;
  return detailMessage || fallback;
}
const POLL_MAX_COUNT = 24; // ~2 min

export function OrderActionsProvider({ children }: { children: ReactNode }) {
  const futuAccountEnv = useFutuAccountEnv();
  const [pendingCancels, setPendingCancels] = useState<Map<string, OpenOrder>>(
    new Map(),
  );
  const [pendingModifies, setPendingModifies] = useState<
    Map<string, PendingModify>
  >(new Map());
  const [cancelledOrders, setCancelledOrders] = useState<CancelledOrder[]>([]);

  const pollTimersRef = useRef<Map<string, ReturnType<typeof setInterval>>>(
    new Map(),
  );
  const pollCountsRef = useRef<Map<string, number>>(new Map());
  const notificationsRef = useRef<Notification[]>([]);
  const ordersUpdaterRef = useRef<((data: OrdersData) => void) | null>(null);
  const pendingModifiesRef = useRef<Map<string, PendingModify>>(new Map());
  const modifySeqCountsRef = useRef<Map<string, number>>(new Map());
  const futuEnvRef = useRef(futuAccountEnv);
  futuEnvRef.current = futuAccountEnv;
  const ordersUpdaterScopeRef = useRef<string | null>(null);
  // Uncertain Futu cancel/modify: the row's terms at the time of the action,
  // keyed by env + brokerOrderId. Cleared by a same-scope snapshot that shows
  // the order gone/terminal or with different terms, or by a page reload.
  const futuUnconfirmedRef = useRef<Map<string, FutuManagementLock>>(new Map());
  const [futuUnconfirmedVersion, setFutuUnconfirmedVersion] = useState(0);

  const markFutuUnconfirmed = useCallback(
    (env: FutuAccountEnv, order: OpenOrder) => {
      if (!order.brokerOrderId) return;
      futuUnconfirmedRef.current.set(
        futuManagementLockKey(env, order.brokerOrderId),
        futuManagementLockFor(env, {
          ...order,
          brokerOrderId: order.brokerOrderId,
        }),
      );
      setFutuUnconfirmedVersion((v) => v + 1);
    },
    [],
  );

  const clearFutuUnconfirmed = useCallback(
    (env: FutuAccountEnv, brokerOrderId: string) => {
      const key = futuManagementLockKey(env, brokerOrderId);
      if (!futuUnconfirmedRef.current.delete(key)) return;
      setFutuUnconfirmedVersion((v) => v + 1);
    },
    [],
  );

  /** Reads the ref, so callers see a lock set earlier in the same tick. */
  const futuLockedNow = useCallback((order: OpenOrder, env: FutuAccountEnv) => {
    if (!order.brokerOrderId) return false;
    const lock = futuUnconfirmedRef.current.get(
      futuManagementLockKey(env, order.brokerOrderId),
    );
    return lock != null && futuLockStillHolds(lock, order);
  }, []);

  const isFutuManagementLocked = useCallback(
    (order: OpenOrder) =>
      futuLockedNow(order, resolveFutuEnv(order, futuAccountEnv)),
    // futuUnconfirmedVersion: re-create so consumers re-render on lock changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [futuAccountEnv, futuLockedNow, futuUnconfirmedVersion],
  );

  const observeOrdersSnapshot = useCallback(
    (scopeKey: string, data: OrdersData) => {
      if (!scopeKey.startsWith("FUTU:")) return;
      const env = scopeKey.slice("FUTU:".length);
      let changed = false;
      for (const [key, lock] of futuUnconfirmedRef.current) {
        if (lock.env !== env) continue;
        const row = data.open_orders.find(
          (o) => o.brokerOrderId === lock.brokerOrderId,
        );
        if (!row || !futuLockStillHolds(lock, row)) {
          futuUnconfirmedRef.current.delete(key);
          changed = true;
        }
      }
      if (changed) setFutuUnconfirmedVersion((v) => v + 1);
    },
    [],
  );

  useEffect(() => {
    pendingModifiesRef.current = pendingModifies;
  }, [pendingModifies]);

  const isPendingCancel = useCallback(
    (order: OpenOrder) => pendingCancels.has(openOrderActionKey(order)),
    [pendingCancels],
  );

  const isPendingModify = useCallback(
    (order: OpenOrder) => pendingModifies.has(openOrderActionKey(order)),
    [pendingModifies],
  );

  const ordersRefreshUrl = useCallback((order: OpenOrder) => {
    if (!isFutuOpenOrder(order)) return "/api/orders";
    const env = resolveFutuEnv(order, futuEnvRef.current);
    return futuOrdersApiPath(env);
  }, []);

  const pushNotification = useCallback((n: Notification) => {
    notificationsRef.current.push(n);
  }, []);

  /** Publish a snapshot only to a view registered for the same scope. */
  const publishOrders = useCallback(
    (data: OrdersData, scopeKey: string, optimistic: boolean) => {
      observeOrdersSnapshot(scopeKey, data);
      if (
        ordersUpdaterScopeRef.current != null &&
        ordersUpdaterScopeRef.current !== scopeKey
      ) {
        return;
      }
      const currentModifies = pendingModifiesRef.current;
      if (!optimistic || currentModifies.size === 0) {
        ordersUpdaterRef.current?.(data);
        return;
      }
      ordersUpdaterRef.current?.({
        ...data,
        open_orders: data.open_orders.map((o) => {
          const pm = currentModifies.get(openOrderActionKey(o));
          if (!pm) return o;
          return {
            ...o,
            limitPrice: pm.newPrice ?? o.limitPrice,
            totalQuantity: pm.newQuantity ?? o.totalQuantity,
          };
        }),
      });
    },
    [observeOrdersSnapshot],
  );

  /** Apply optimistic modify prices before pushing data to the UI */
  const pushOrdersData = useCallback(
    (data: OrdersData, scopeKey: string) => publishOrders(data, scopeKey, true),
    [publishOrders],
  );

  /** Remove a pending modify from the ref now (not after the next render), so
   *  a snapshot published in the same tick carries no stale overlay. */
  const clearPendingModify = useCallback((actionKey: string) => {
    if (pendingModifiesRef.current.has(actionKey)) {
      const next = new Map(pendingModifiesRef.current);
      next.delete(actionKey);
      pendingModifiesRef.current = next;
    }
    setPendingModifies((prev) => {
      if (!prev.has(actionKey)) return prev;
      const next = new Map(prev);
      next.delete(actionKey);
      return next;
    });
  }, []);

  /** Refetch the currently selected orders view (only that view). */
  const refreshCurrentView = useCallback(async () => {
    const scopeKey = ordersUpdaterScopeRef.current;
    if (scopeKey == null) return;
    const url = scopeKey.startsWith("FUTU:")
      ? futuOrdersApiPath(scopeKey.slice("FUTU:".length) as FutuAccountEnv)
      : "/api/orders";
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) return;
      pushOrdersData((await res.json()) as OrdersData, scopeKey);
    } catch {
      // The view's own polling recovers; nothing to publish.
    }
  }, [pushOrdersData]);

  const refreshOrders = useCallback(
    async (order: OpenOrder) => {
      const res = await fetch(ordersRefreshUrl(order), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) return null;
      return (await res.json()) as OrdersData;
    },
    [ordersRefreshUrl],
  );

  /* ── Cancel polling ─────────────────────────────────── */

  const startCancelPoll = useCallback(
    (order: OpenOrder) => {
      if (isFutuOpenOrder(order)) return;
      const actionKey = openOrderActionKey(order);
      pollCountsRef.current.set(actionKey, 0);

      const tick = async () => {
        const count = (pollCountsRef.current.get(actionKey) ?? 0) + 1;
        pollCountsRef.current.set(actionKey, count);

        try {
          const data = await refreshOrders(order);
          if (!data) {
            scheduleNext();
            return;
          }

          const stillOpen = orderStillOpen(data, order);

          if (!stillOpen) {
            pollTimersRef.current.delete(actionKey);
            pollCountsRef.current.delete(actionKey);

            setPendingCancels((prev) => {
              const next = new Map(prev);
              next.delete(actionKey);
              return next;
            });
            setCancelledOrders((prev) => [
              {
                permId: order.permId,
                symbol: order.symbol,
                action: order.action,
                orderType: order.orderType,
                totalQuantity: order.totalQuantity,
                limitPrice: order.limitPrice,
                cancelledAt: new Date().toISOString(),
              },
              ...prev,
            ]);

            pushOrdersData(data, ordersScopeKey(order, futuEnvRef.current));
            pushNotification({
              type: "success",
              message: `${order.symbol} order cancelled`,
            });
          } else if (count >= POLL_MAX_COUNT) {
            pollTimersRef.current.delete(actionKey);
            pollCountsRef.current.delete(actionKey);
            setPendingCancels((prev) => {
              const next = new Map(prev);
              next.delete(actionKey);
              return next;
            });
            pushOrdersData(data, ordersScopeKey(order, futuEnvRef.current));
            pushNotification({
              type: "error",
              message: `${order.symbol} cancellation failed — order still open. Try cancelling again.`,
              duration: 0,
            });
          } else {
            pushOrdersData(data, ordersScopeKey(order, futuEnvRef.current));
            scheduleNext();
          }
        } catch {
          scheduleNext();
        }
      };

      const scheduleNext = () => {
        const timer = setTimeout(tick, POLL_INTERVAL_MS);
        pollTimersRef.current.set(actionKey, timer);
      };

      scheduleNext();
    },
    [pushNotification, pushOrdersData, refreshOrders],
  );

  const requestCancel = useCallback(
    async (order: OpenOrder): Promise<ActionResult> => {
      const actionKey = openOrderActionKey(order);
      const futu = isFutuOpenOrder(order);
      const env = resolveFutuEnv(order, futuAccountEnv);
      const scopeKey = ordersScopeKey(order, futuAccountEnv);
      const cancelUrl = futu ? futuCancelApiPath(env) : "/api/orders/cancel";
      const cancelBody =
        futu && order.brokerOrderId
          ? { brokerOrderId: order.brokerOrderId }
          : { orderId: order.orderId, permId: order.permId };
      if (futu && futuLockedNow(order, env)) {
        const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
        pushNotification({ type: "error", message, duration: 0 });
        return {
          ok: false,
          status: 409,
          reasonCode: "FUTU_SEND_UNCERTAIN",
          message,
        };
      }
      // Every Futu cancel outcome refetches the currently selected view.
      if (futu) {
        try {
          return await sendFutuCancel();
        } finally {
          void refreshCurrentView();
        }
      }
      try {
        const res = await fetch(cancelUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(cancelBody),
        });
        const json = (await res.json().catch(() => ({}))) as Record<
          string,
          unknown
        >;
        if (!res.ok) return failure(res.status, json, "Cancel failed");
        setPendingCancels((prev) => new Map(prev).set(actionKey, order));
        startCancelPoll(order);
        if (json.orders) {
          pushOrdersData(json.orders as OrdersData, scopeKey);
        }
        return { ok: true };
      } catch {
        const message = "Cancel request failed";
        pushNotification({ type: "error", message });
        return { ok: false, status: 0, reasonCode: null, message };
      }

      async function sendFutuCancel(): Promise<ActionResult> {
        let res: Response;
        let json: Record<string, unknown>;
        try {
          res = await fetch(cancelUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(cancelBody),
          });
          json = (await res.json().catch(() => ({}))) as Record<
            string,
            unknown
          >;
        } catch {
          markFutuUnconfirmed(env, order);
          const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
          pushNotification({ type: "error", message, duration: 0 });
          return {
            ok: false,
            status: 0,
            reasonCode: "FUTU_SEND_UNCERTAIN",
            message,
          };
        }
        if (!res.ok) {
          if (isUncertainFutuManagementResponse(res.status, json)) {
            markFutuUnconfirmed(env, order);
            const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
            pushNotification({ type: "error", message, duration: 0 });
            return {
              ok: false,
              status: res.status,
              reasonCode: readReasonCode(json),
              message,
            };
          }
          return failure(res.status, json, "Cancel failed");
        }
        if (order.brokerOrderId) clearFutuUnconfirmed(env, order.brokerOrderId);
        const state = String(json.state ?? "").toUpperCase();
        const confirmed = json.confirmed;
        if (confirmed === true && state === "CANCELLED") {
          pushNotification({
            type: "success",
            message: `${order.symbol} order cancelled`,
          });
          return { ok: true };
        }
        if (state === "FILLED") {
          pushNotification({
            type: "success",
            message: `${order.symbol} order filled`,
          });
          return { ok: true };
        }
        if (state === "REJECTED") {
          const message = `${order.symbol} cancel rejected by Futu`;
          pushNotification({ type: "error", message });
          return { ok: false, status: 409, reasonCode: "IB_REJECT", message };
        }
        if (confirmed === false) {
          pushNotification({
            type: "warning",
            message: FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
          });
          return { ok: true };
        }
        pushNotification({
          type: "success",
          message: `${order.symbol} cancel accepted`,
        });
        return { ok: true };
      }

      function failure(
        status: number,
        json: Record<string, unknown>,
        fallback: string,
      ): ActionResult {
        const reasonCode = readReasonCode(json);
        const message = errorMessageFromBody(json, reasonCode, fallback);
        pushNotification({ type: "error", message });
        return { ok: false, status, reasonCode, message };
      }
    },
    [
      pushNotification,
      startCancelPoll,
      pushOrdersData,
      futuAccountEnv,
      refreshCurrentView,
      futuLockedNow,
      markFutuUnconfirmed,
      clearFutuUnconfirmed,
    ],
  );

  /* ── Modify polling ─────────────────────────────────── */

  const startModifyPoll = useCallback(
    (order: OpenOrder, request: ModifyRequestWithOutsideRth) => {
      if (isFutuOpenOrder(order)) return;
      const actionKey = openOrderActionKey(order);
      pollCountsRef.current.set(actionKey, 0);

      const tick = async () => {
        const count = (pollCountsRef.current.get(actionKey) ?? 0) + 1;
        pollCountsRef.current.set(actionKey, count);

        try {
          const data = await refreshOrders(order);
          if (!data) {
            scheduleNext();
            return;
          }

          const liveOrder = findOpenOrderInSnapshot(data, order);
          const confirmed = Boolean(
            liveOrder &&
            (request.newPrice == null ||
              (liveOrder.limitPrice != null &&
                Math.abs(liveOrder.limitPrice - request.newPrice) < 0.001)) &&
            (request.newQuantity == null ||
              liveOrder.totalQuantity === request.newQuantity),
          );

          if (confirmed) {
            pollTimersRef.current.delete(actionKey);
            pollCountsRef.current.delete(actionKey);
            clearPendingModify(actionKey);
            publishOrders(
              data,
              ordersScopeKey(order, futuEnvRef.current),
              false,
            );
            const messageParts: string[] = [];
            if (request.newPrice != null) {
              messageParts.push(`price $${request.newPrice.toFixed(2)}`);
            }
            if (request.newQuantity != null) {
              messageParts.push(`qty ${request.newQuantity}`);
            }
            pushNotification({
              type: "success",
              message:
                messageParts.length > 0
                  ? `${order.symbol} order confirmed (${messageParts.join(", ")})`
                  : `${order.symbol} order confirmed`,
            });
          } else if (count >= POLL_MAX_COUNT) {
            pollTimersRef.current.delete(actionKey);
            pollCountsRef.current.delete(actionKey);
            // The timed-out terms must not overlay the broker's actual row.
            clearPendingModify(actionKey);
            publishOrders(
              data,
              ordersScopeKey(order, futuEnvRef.current),
              false,
            );
            pushNotification({
              type: "error",
              message: `${order.symbol} modify not confirmed by IB`,
              duration: 0,
            });
          } else {
            pushOrdersData(data, ordersScopeKey(order, futuEnvRef.current));
            scheduleNext();
          }
        } catch {
          scheduleNext();
        }
      };

      const scheduleNext = () => {
        const timer = setTimeout(tick, POLL_INTERVAL_MS);
        pollTimersRef.current.set(actionKey, timer);
      };

      scheduleNext();
    },
    [
      pushNotification,
      pushOrdersData,
      publishOrders,
      clearPendingModify,
      refreshOrders,
    ],
  );

  const replaceAttemptsRef = useRef(
    new Map<number, { id: string; uncertain: boolean }>(),
  );
  const [lockedReplacements, setLockedReplacements] = useState<Set<number>>(
    new Set(),
  );

  const requestReplace = useCallback(
    async (
      order: OpenOrder,
      request: ModifyRequestWithOutsideRth,
    ): Promise<ActionResult> => {
      const key = order.permId;
      const uncertain = (): ActionResult => {
        replaceAttemptsRef.current.set(key, {
          id: replaceAttemptsRef.current.get(key)?.id ?? "",
          uncertain: true,
        });
        setLockedReplacements((prev) => new Set(prev).add(key));
        pushNotification({
          type: "error",
          message: REPLACEMENT_UNCERTAIN_COPY,
        });
        return {
          ok: false,
          status: 0,
          reasonCode: "PLACE_OUTCOME_UNKNOWN",
          message: REPLACEMENT_UNCERTAIN_COPY,
        };
      };
      const prior = replaceAttemptsRef.current.get(key);
      if (prior?.uncertain) return uncertain();
      const attemptId = prior?.id || randomAttemptId();
      replaceAttemptsRef.current.set(key, { id: attemptId, uncertain: false });

      const { outsideRth, ...requestBody } = request;
      const seqKey = openOrderActionKey(order);
      const nextSeq = computeNextModifySequence(
        order,
        seqKey,
        modifySeqCountsRef.current,
      );
      modifySeqCountsRef.current.set(seqKey, nextSeq);
      let res: Response;
      try {
        res = await fetch("/api/orders/modify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            orderId: order.orderId,
            permId: order.permId,
            ...requestBody,
            replaceOrder: {
              ...request.replaceOrder,
              client_attempt_id: attemptId,
            },
            outsideRth,
            modifySequence: nextSeq,
          }),
        });
      } catch {
        return uncertain();
      }
      let json: Record<string, unknown> | null = null;
      try {
        const parsed: unknown = await res.json();
        json =
          parsed && typeof parsed === "object"
            ? (parsed as Record<string, unknown>)
            : null;
      } catch {
        json = null;
      }
      // Combo replacement is IB-only: publish the authoritative IB snapshot
      // (no optimistic overlay) only if the IB view is still selected.
      void fetch("/api/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => data && publishOrders(data as OrdersData, "IB", false))
        .catch(() => undefined);
      if (res.ok && json) {
        replaceAttemptsRef.current.delete(key);
        pushNotification({
          type: "success",
          message: `${order.symbol} order replaced`,
        });
        return { ok: true };
      }
      if (res.ok || isUncertainPlaceResponse(res.status, json))
        return uncertain();
      replaceAttemptsRef.current.delete(key);
      const reasonCode = readReasonCode(json);
      const message =
        (typeof json?.error === "string" && json.error) ||
        (reasonCode ? getReasonToast(reasonCode).copy : "Replacement failed");
      pushNotification({ type: "error", message });
      return { ok: false, status: res.status, reasonCode, message };
    },
    [pushNotification, publishOrders],
  );

  const requestModify = useCallback(
    async (
      order: OpenOrder,
      request: ModifyRequestWithOutsideRth,
    ): Promise<ActionResult> => {
      if (isFutuOpenOrder(order) && request.replaceOrder) {
        const message = "Combo replace is not supported on Futu.";
        pushNotification({ type: "error", message });
        return {
          ok: false,
          status: 403,
          reasonCode: "READ_ONLY_BROKER",
          message,
        };
      }
      if (request.replaceOrder) return requestReplace(order, request);
      const actionKey = openOrderActionKey(order);
      const futu = isFutuOpenOrder(order);
      const env = resolveFutuEnv(order, futuAccountEnv);
      const scopeKey = ordersScopeKey(order, futuAccountEnv);
      const modifyUrl = futu ? futuModifyApiPath(env) : "/api/orders/modify";
      if (futu && futuLockedNow(order, env)) {
        const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
        pushNotification({ type: "error", message, duration: 0 });
        return {
          ok: false,
          status: 409,
          reasonCode: "FUTU_SEND_UNCERTAIN",
          message,
        };
      }
      const {
        outsideRth,
        newAuxPrice,
        newTrailValue,
        newTrailSpread,
        ...baseRequest
      } = request;
      const nextSeq = computeNextModifySequence(
        order,
        actionKey,
        modifySeqCountsRef.current,
      );
      modifySeqCountsRef.current.set(actionKey, nextSeq);
      const modifyBody =
        futu && order.brokerOrderId
          ? {
              brokerOrderId: order.brokerOrderId,
              ...baseRequest,
              // FUTU only (contract § Modify); an IB body never carries them.
              newAuxPrice,
              newTrailValue,
              newTrailSpread,
              modifySequence: nextSeq,
            }
          : {
              orderId: order.orderId,
              permId: order.permId,
              ...baseRequest,
              outsideRth,
              modifySequence: nextSeq,
            };
      // Futu trigger / trail edits are field changes too (A6): an aux-only
      // or trail-only modify must not report success unless confirmed.
      const hasFieldChange =
        request.newPrice != null ||
        request.newQuantity != null ||
        (futu &&
          (request.newAuxPrice != null ||
            request.newTrailValue != null ||
            request.newTrailSpread != null));

      const recordStale = (json: Record<string, unknown>) => {
        const detail = json?.detail;
        const applied =
          detail && typeof detail === "object"
            ? (detail as Record<string, unknown>).applied
            : undefined;
        if (typeof applied === "number") {
          modifySeqCountsRef.current.set(actionKey, applied);
        }
      };
      const recordApplied = (json: Record<string, unknown>) => {
        const appliedSeq = json?.applied_sequence;
        recordAppliedModifySequence(
          actionKey,
          modifySeqCountsRef.current,
          typeof appliedSeq === "number" ? appliedSeq : undefined,
          nextSeq,
        );
      };
      const failure = (
        status: number,
        json: Record<string, unknown>,
      ): ActionResult => {
        const reasonCode = readReasonCode(json);
        if (reasonCode === "MODIFY_STALE") recordStale(json);
        const message = errorMessageFromBody(json, reasonCode, "Modify failed");
        pushNotification({ type: "error", message });
        return { ok: false, status, reasonCode, message };
      };
      const confirmedMessage = () => {
        if (futu) {
          const terms = futuModifyTermsText(request, order.trailType);
          return terms
            ? `${order.symbol} order confirmed (${terms})`
            : `${order.symbol} order confirmed`;
        }
        const parts: string[] = [];
        if (request.newPrice != null) {
          parts.push(`price $${request.newPrice.toFixed(2)}`);
        }
        if (request.newQuantity != null)
          parts.push(`qty ${request.newQuantity}`);
        return parts.length > 0
          ? `${order.symbol} order confirmed (${parts.join(", ")})`
          : `${order.symbol} order confirmed`;
      };

      if (futu) {
        // Futu: the server response is authoritative. No IB pending map or
        // poll; every outcome refetches the currently selected view.
        const uncertain = (status: number, reasonCode: string | null) => {
          markFutuUnconfirmed(env, order);
          const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
          pushNotification({ type: "error", message, duration: 0 });
          return { ok: false as const, status, reasonCode, message };
        };
        try {
          let res: Response;
          let json: Record<string, unknown>;
          try {
            res = await fetch(modifyUrl, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(modifyBody),
            });
            json = (await res.json().catch(() => ({}))) as Record<
              string,
              unknown
            >;
          } catch {
            return uncertain(0, "FUTU_SEND_UNCERTAIN");
          }
          if (!res.ok) {
            if (isUncertainFutuManagementResponse(res.status, json)) {
              return uncertain(res.status, readReasonCode(json));
            }
            return failure(res.status, json);
          }
          recordApplied(json);
          if (order.brokerOrderId) {
            clearFutuUnconfirmed(env, order.brokerOrderId);
          }
          if (!hasFieldChange) {
            pushNotification({
              type: "success",
              message: `${order.symbol} order modified`,
            });
          } else if (json.confirmed === true) {
            pushNotification({ type: "success", message: confirmedMessage() });
          } else {
            pushNotification({
              type: "warning",
              message: FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
            });
          }
          return { ok: true };
        } finally {
          void refreshCurrentView();
        }
      }

      try {
        const res = await fetch(modifyUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(modifyBody),
        });
        const json = (await res.json().catch(() => ({}))) as Record<
          string,
          unknown
        >;
        if (!res.ok) return failure(res.status, json);
        recordApplied(json);
        if (hasFieldChange) {
          const pm: PendingModify = {
            order,
            newPrice: request.newPrice,
            newQuantity: request.newQuantity,
          };
          setPendingModifies((prev) => new Map(prev).set(actionKey, pm));
        }
        if (json.orders) {
          pushOrdersData(json.orders as OrdersData, scopeKey);
        }
        if (hasFieldChange) {
          startModifyPoll(order, request);
        } else {
          pushNotification({
            type: "success",
            message: `${order.symbol} order modified`,
          });
        }
        return { ok: true };
      } catch {
        const message = "Modify request failed";
        pushNotification({ type: "error", message });
        return { ok: false, status: 0, reasonCode: null, message };
      }
    },
    [
      pushNotification,
      startModifyPoll,
      pushOrdersData,
      requestReplace,
      futuAccountEnv,
      futuLockedNow,
      refreshCurrentView,
      markFutuUnconfirmed,
      clearFutuUnconfirmed,
    ],
  );

  const drainNotifications = useCallback((): Notification[] => {
    if (notificationsRef.current.length === 0) return [];
    const batch = notificationsRef.current;
    notificationsRef.current = [];
    return batch;
  }, []);

  const setOrdersUpdater = useCallback(
    (
      fn: ((data: OrdersData) => void) | null,
      scopeKey: string | null = null,
    ) => {
      ordersUpdaterRef.current = fn;
      ordersUpdaterScopeRef.current = scopeKey;
    },
    [],
  );

  useEffect(() => {
    return () => {
      for (const timer of pollTimersRef.current.values()) {
        clearTimeout(timer);
      }
    };
  }, []);

  return (
    <OrderActionsContext.Provider
      value={{
        pendingCancels,
        pendingModifies,
        cancelledOrders,
        isPendingCancel,
        isPendingModify,
        requestCancel,
        requestModify,
        drainNotifications,
        setOrdersUpdater,
        lockedReplacements,
        isFutuManagementLocked,
        observeOrdersSnapshot,
      }}
    >
      {children}
    </OrderActionsContext.Provider>
  );
}

/** Non-throwing accessor for hooks that may run outside the provider. */
export function useOrderActionsOptional(): OrderActionsContextValue | null {
  return useContext(OrderActionsContext);
}

export function useOrderActions(): OrderActionsContextValue {
  const ctx = useContext(OrderActionsContext);
  if (!ctx)
    throw new Error("useOrderActions must be used within OrderActionsProvider");
  return ctx;
}
