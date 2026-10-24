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
  futuManagementLockKey,
  futuModifyApiPath,
  futuOrdersApiPath,
  isFutuOpenOrder,
  openOrderActionKey,
  orderStillOpen,
  recordAppliedModifySequence,
} from "@/lib/futuOpenOrder";
import { ordersScopeKey, resolveFutuEnv } from "@/lib/orderActionScope";
import { isUncertainFutuManagementResponse } from "@/lib/futuManagementUncertain";

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
  /** permIds whose combo replacement ended uncertain: the replacement form
   * for that order is locked (no retry) until the page reloads. */
  lockedReplacements: Set<number>;
};

const OrderActionsContext = createContext<OrderActionsContextValue | null>(
  null,
);

const POLL_INTERVAL_MS = 5_000;
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
  const futuUnconfirmedRef = useRef<Set<string>>(new Set());
  const [futuUnconfirmedKeys, setFutuUnconfirmedKeys] = useState<Set<string>>(
    () => new Set(),
  );

  const markFutuUnconfirmed = useCallback(
    (env: FutuAccountEnv, brokerOrderId: string) => {
      const key = futuManagementLockKey(env, brokerOrderId);
      futuUnconfirmedRef.current.add(key);
      setFutuUnconfirmedKeys(new Set(futuUnconfirmedRef.current));
    },
    [],
  );

  const clearFutuUnconfirmed = useCallback(
    (env: FutuAccountEnv, brokerOrderId: string) => {
      const key = futuManagementLockKey(env, brokerOrderId);
      if (!futuUnconfirmedRef.current.delete(key)) return;
      setFutuUnconfirmedKeys(new Set(futuUnconfirmedRef.current));
    },
    [],
  );

  const isFutuManagementLocked = useCallback(
    (order: OpenOrder) => {
      if (!order.brokerOrderId) return false;
      const env = resolveFutuEnv(order, futuAccountEnv);
      return futuUnconfirmedRef.current.has(
        futuManagementLockKey(env, order.brokerOrderId),
      );
    },
    [futuAccountEnv, futuUnconfirmedKeys],
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

  /** Apply optimistic modify prices before pushing data to the UI */
  const pushOrdersData = useCallback((data: OrdersData, scopeKey: string) => {
    if (
      ordersUpdaterScopeRef.current != null &&
      ordersUpdaterScopeRef.current !== scopeKey
    ) {
      return;
    }
    const currentModifies = pendingModifiesRef.current;
    if (currentModifies.size === 0) {
      ordersUpdaterRef.current?.(data);
      return;
    }
    const patched: OrdersData = {
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
    };
    ordersUpdaterRef.current?.(patched);
  }, []);

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
      if (
        futu &&
        order.brokerOrderId &&
        futuUnconfirmedRef.current.has(
          futuManagementLockKey(env, order.brokerOrderId),
        )
      ) {
        const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
        pushNotification({ type: "error", message, duration: 0 });
        return {
          ok: false,
          status: 409,
          reasonCode: "FUTU_SEND_UNCERTAIN",
          message,
        };
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
        if (!res.ok) {
          if (
            futu &&
            order.brokerOrderId &&
            isUncertainFutuManagementResponse(res.status, json)
          ) {
            markFutuUnconfirmed(env, order.brokerOrderId);
            const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
            pushNotification({ type: "error", message, duration: 0 });
            return {
              ok: false,
              status: res.status,
              reasonCode: readReasonCode(json),
              message,
            };
          }
          const reasonCode = readReasonCode(json);
          const message = reasonCode
            ? getReasonToast(reasonCode).copy
            : (typeof json?.error === "string" ? json.error : null) ||
              (typeof json?.detail === "string"
                ? json.detail
                : (json?.detail as { message?: string } | undefined)
                    ?.message) ||
              "Cancel failed";
          pushNotification({ type: "error", message });
          return { ok: false, status: res.status, reasonCode, message };
        }

        if (futu) {
          if (json.orders) {
            pushOrdersData(json.orders as OrdersData, scopeKey);
          } else {
            const data = await refreshOrders(order);
            if (data) pushOrdersData(data, scopeKey);
          }
          const state = String(json.state ?? "").toUpperCase();
          const confirmed = json.confirmed;
          if (order.brokerOrderId) {
            clearFutuUnconfirmed(env, order.brokerOrderId);
          }
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

        setPendingCancels((prev) => new Map(prev).set(actionKey, order));
        startCancelPoll(order);
        if (json.orders) {
          pushOrdersData(json.orders as OrdersData, scopeKey);
        }
        return { ok: true };
      } catch {
        if (futu && order.brokerOrderId) {
          markFutuUnconfirmed(env, order.brokerOrderId);
          const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
          pushNotification({ type: "error", message, duration: 0 });
          return {
            ok: false,
            status: 0,
            reasonCode: "FUTU_SEND_UNCERTAIN",
            message,
          };
        }
        const message = "Cancel request failed";
        pushNotification({ type: "error", message });
        return { ok: false, status: 0, reasonCode: null, message };
      }
    },
    [
      pushNotification,
      startCancelPoll,
      pushOrdersData,
      futuAccountEnv,
      refreshOrders,
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

            setPendingModifies((prev) => {
              const next = new Map(prev);
              next.delete(actionKey);
              return next;
            });

            pushOrdersData(data, ordersScopeKey(order, futuEnvRef.current));
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

            setPendingModifies((prev) => {
              const next = new Map(prev);
              next.delete(actionKey);
              return next;
            });

            pushOrdersData(data, ordersScopeKey(order, futuEnvRef.current));
            if (!isFutuOpenOrder(order)) {
              pushNotification({
                type: "error",
                message: `${order.symbol} modify not confirmed by IB`,
                duration: 0,
              });
            }
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
      void fetch("/api/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => data && ordersUpdaterRef.current?.(data as OrdersData))
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
    [pushNotification],
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
      if (
        futu &&
        order.brokerOrderId &&
        futuUnconfirmedRef.current.has(
          futuManagementLockKey(env, order.brokerOrderId),
        )
      ) {
        const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
        pushNotification({ type: "error", message, duration: 0 });
        return {
          ok: false,
          status: 409,
          reasonCode: "FUTU_SEND_UNCERTAIN",
          message,
        };
      }
      try {
        const { outsideRth, ...requestBody } = request;
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
                ...requestBody,
                modifySequence: nextSeq,
              }
            : {
                orderId: order.orderId,
                permId: order.permId,
                ...requestBody,
                outsideRth,
                modifySequence: nextSeq,
              };
        const res = await fetch(modifyUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(modifyBody),
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok) {
          const reasonCode = readReasonCode(json);
          if (
            futu &&
            order.brokerOrderId &&
            isUncertainFutuManagementResponse(res.status, json)
          ) {
            markFutuUnconfirmed(env, order.brokerOrderId);
            const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
            pushNotification({ type: "error", message, duration: 0 });
            return {
              ok: false,
              status: res.status,
              reasonCode,
              message,
            };
          }
          if (reasonCode === "MODIFY_STALE") {
            const detail = (json as { detail?: unknown })?.detail;
            const applied =
              detail && typeof detail === "object"
                ? (detail as Record<string, unknown>).applied
                : undefined;
            if (typeof applied === "number") {
              modifySeqCountsRef.current.set(actionKey, applied);
            }
          }
          const message = reasonCode
            ? getReasonToast(reasonCode).copy
            : (typeof json?.error === "string" ? json.error : null) ||
              (typeof json?.detail === "string"
                ? json.detail
                : (json?.detail as { message?: string } | undefined)
                    ?.message) ||
              "Modify failed";
          pushNotification({ type: "error", message });
          return { ok: false, status: res.status, reasonCode, message };
        }
        const appliedSeq = (json as { applied_sequence?: unknown })
          ?.applied_sequence;
        recordAppliedModifySequence(
          actionKey,
          modifySeqCountsRef.current,
          typeof appliedSeq === "number" ? appliedSeq : undefined,
          nextSeq,
        );
        if (request.newPrice != null || request.newQuantity != null) {
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

        const hasFieldChange =
          request.newPrice != null || request.newQuantity != null;

        if (futu && hasFieldChange) {
          const serverConfirmed = (json as { confirmed?: unknown }).confirmed;
          if (order.brokerOrderId) {
            clearFutuUnconfirmed(env, order.brokerOrderId);
          }
          if (serverConfirmed === true) {
            setPendingModifies((prev) => {
              const next = new Map(prev);
              next.delete(actionKey);
              return next;
            });
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
          } else if (serverConfirmed === false) {
            pushNotification({
              type: "warning",
              message: FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
            });
          } else {
            pushNotification({
              type: "warning",
              message: FUTU_MODIFY_SENT_UNCONFIRMED_COPY,
            });
          }
        } else if (hasFieldChange) {
          startModifyPoll(order, request);
        } else {
          pushNotification({
            type: "success",
            message: `${order.symbol} order modified`,
          });
        }
        return { ok: true };
      } catch {
        if (futu && order.brokerOrderId) {
          markFutuUnconfirmed(env, order.brokerOrderId);
          const message = FUTU_MANAGEMENT_UNCONFIRMED_COPY;
          pushNotification({ type: "error", message, duration: 0 });
          return {
            ok: false,
            status: 0,
            reasonCode: "FUTU_SEND_UNCERTAIN",
            message,
          };
        }
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
      }}
    >
      {children}
    </OrderActionsContext.Provider>
  );
}

export function useOrderActions(): OrderActionsContextValue {
  const ctx = useContext(OrderActionsContext);
  if (!ctx)
    throw new Error("useOrderActions must be used within OrderActionsProvider");
  return ctx;
}
