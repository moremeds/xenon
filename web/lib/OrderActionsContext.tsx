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
  pendingCancels: Map<number, OpenOrder>;
  pendingModifies: Map<number, PendingModify>;
  cancelledOrders: CancelledOrder[];
  requestCancel: (order: OpenOrder) => Promise<ActionResult>;
  requestModify: (
    order: OpenOrder,
    request: ModifyOrderRequest,
  ) => Promise<ActionResult>;
  drainNotifications: () => Notification[];
  setOrdersUpdater: (fn: ((data: OrdersData) => void) | null) => void;
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
  const [pendingCancels, setPendingCancels] = useState<Map<number, OpenOrder>>(
    new Map(),
  );
  const [pendingModifies, setPendingModifies] = useState<
    Map<number, PendingModify>
  >(new Map());
  const [cancelledOrders, setCancelledOrders] = useState<CancelledOrder[]>([]);

  const pollTimersRef = useRef<Map<number, ReturnType<typeof setInterval>>>(
    new Map(),
  );
  const pollCountsRef = useRef<Map<number, number>>(new Map());
  const notificationsRef = useRef<Notification[]>([]);
  const ordersUpdaterRef = useRef<((data: OrdersData) => void) | null>(null);
  const pendingModifiesRef = useRef<Map<number, PendingModify>>(new Map());
  const modifySeqCountsRef = useRef<Map<number, number>>(new Map());

  // Keep ref in sync with state for use inside interval callbacks
  useEffect(() => {
    pendingModifiesRef.current = pendingModifies;
  }, [pendingModifies]);

  const pushNotification = useCallback((n: Notification) => {
    notificationsRef.current.push(n);
  }, []);

  /** Apply optimistic modify prices before pushing data to the UI */
  const pushOrdersData = useCallback((data: OrdersData) => {
    const currentModifies = pendingModifiesRef.current;
    if (currentModifies.size === 0) {
      ordersUpdaterRef.current?.(data);
      return;
    }
    const patched: OrdersData = {
      ...data,
      open_orders: data.open_orders.map((o) => {
        const pm = currentModifies.get(o.permId);
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

  /* ── Cancel polling ─────────────────────────────────── */

  const startCancelPoll = useCallback(
    (order: OpenOrder) => {
      const permId = order.permId;
      pollCountsRef.current.set(permId, 0);

      const tick = async () => {
        const count = (pollCountsRef.current.get(permId) ?? 0) + 1;
        pollCountsRef.current.set(permId, count);

        try {
          const res = await fetch("/api/orders", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
          });
          if (!res.ok) {
            scheduleNext();
            return;
          }
          const data = (await res.json()) as OrdersData;

          const stillOpen = data.open_orders.some(
            (o) =>
              o.permId === permId ||
              (o.orderId === order.orderId && order.orderId !== 0),
          );

          if (!stillOpen) {
            pollTimersRef.current.delete(permId);
            pollCountsRef.current.delete(permId);

            setPendingCancels((prev) => {
              const next = new Map(prev);
              next.delete(permId);
              return next;
            });
            setCancelledOrders((prev) => [
              {
                permId,
                symbol: order.symbol,
                action: order.action,
                orderType: order.orderType,
                totalQuantity: order.totalQuantity,
                limitPrice: order.limitPrice,
                cancelledAt: new Date().toISOString(),
              },
              ...prev,
            ]);

            pushOrdersData(data);
            pushNotification({
              type: "success",
              message: `${order.symbol} order cancelled`,
            });
          } else if (count >= POLL_MAX_COUNT) {
            pollTimersRef.current.delete(permId);
            pollCountsRef.current.delete(permId);
            setPendingCancels((prev) => {
              const next = new Map(prev);
              next.delete(permId);
              return next;
            });
            pushOrdersData(data);
            pushNotification({
              type: "error",
              message: `${order.symbol} cancellation failed — order still open. Try cancelling again.`,
              duration: 0,
            });
          } else {
            pushOrdersData(data);
            scheduleNext();
          }
        } catch {
          scheduleNext();
        }
      };

      const scheduleNext = () => {
        const timer = setTimeout(tick, POLL_INTERVAL_MS);
        pollTimersRef.current.set(permId, timer);
      };

      scheduleNext();
    },
    [pushNotification, pushOrdersData],
  );

  const requestCancel = useCallback(
    async (order: OpenOrder): Promise<ActionResult> => {
      try {
        const res = await fetch("/api/orders/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            orderId: order.orderId,
            permId: order.permId,
          }),
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok) {
          // F6.2: on 503/409 the server refused authoritatively — DO NOT
          // optimistically mark the order cancelled. Surface reason-code copy
          // and let the caller render a FAILED pill; the button re-enables.
          const reasonCode = readReasonCode(json);
          // No reason_code → still surface human-readable upstream detail:
          // FastAPI HTTPException bodies carry it as a string or as
          // detail.message — passThroughXenonError forwards either verbatim.
          const message = reasonCode
            ? getReasonToast(reasonCode).copy
            : json?.error ||
              (typeof json?.detail === "string"
                ? json.detail
                : json?.detail?.message) ||
              "Cancel failed";
          pushNotification({ type: "error", message });
          return { ok: false, status: res.status, reasonCode, message };
        }
        setPendingCancels((prev) => new Map(prev).set(order.permId, order));
        startCancelPoll(order);
        if (json.orders) pushOrdersData(json.orders);
        return { ok: true };
      } catch {
        const message = "Cancel request failed";
        pushNotification({ type: "error", message });
        return { ok: false, status: 0, reasonCode: null, message };
      }
    },
    [pushNotification, startCancelPoll, pushOrdersData],
  );

  /* ── Modify polling ─────────────────────────────────── */

  const startModifyPoll = useCallback(
    (order: OpenOrder, request: ModifyRequestWithOutsideRth) => {
      const permId = order.permId;
      pollCountsRef.current.set(permId, 0);

      const tick = async () => {
        const count = (pollCountsRef.current.get(permId) ?? 0) + 1;
        pollCountsRef.current.set(permId, count);

        try {
          const res = await fetch("/api/orders", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
          });
          if (!res.ok) {
            scheduleNext();
            return;
          }
          const data = (await res.json()) as OrdersData;

          const ibOrder = data.open_orders.find((o) => o.permId === permId);

          const priceConfirmed =
            request.newPrice == null ||
            (ibOrder?.limitPrice != null &&
              Math.abs(ibOrder.limitPrice - request.newPrice) < 0.001);
          const quantityConfirmed =
            request.newQuantity == null ||
            ibOrder?.totalQuantity === request.newQuantity;
          const confirmed = Boolean(
            ibOrder && priceConfirmed && quantityConfirmed,
          );

          if (confirmed) {
            pollTimersRef.current.delete(permId);
            pollCountsRef.current.delete(permId);

            setPendingModifies((prev) => {
              const next = new Map(prev);
              next.delete(permId);
              return next;
            });

            // Push the real data (no overlay needed — IB already shows new price)
            ordersUpdaterRef.current?.(data);
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
            pollTimersRef.current.delete(permId);
            pollCountsRef.current.delete(permId);

            setPendingModifies((prev) => {
              const next = new Map(prev);
              next.delete(permId);
              return next;
            });

            // Push fresh IB data without overlay — reverts to the real (old) price
            ordersUpdaterRef.current?.(data);
            pushNotification({
              type: "error",
              message: `${order.symbol} modify not confirmed by IB`,
              duration: 0,
            });
          } else {
            // Still pending — push data with optimistic overlay
            pushOrdersData(data);
            scheduleNext();
          }
        } catch {
          scheduleNext();
        }
      };

      const scheduleNext = () => {
        const timer = setTimeout(tick, POLL_INTERVAL_MS);
        pollTimersRef.current.set(permId, timer);
      };

      scheduleNext();
    },
    [pushNotification, pushOrdersData],
  );

  // Combo replacement = cancel original + place successor (PR-07, OP-1).
  // One client_attempt_id per logical replacement, keyed to the target
  // order: kept while that replacement is in flight or uncertain, dropped
  // after a resolved result, minted fresh for a new target.
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
        pushNotification({ type: "error", message: REPLACEMENT_UNCERTAIN_COPY });
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
      const nextSeq = (modifySeqCountsRef.current.get(order.permId) ?? 0) + 1;
      modifySeqCountsRef.current.set(order.permId, nextSeq);
      let res: Response;
      try {
        res = await fetch("/api/orders/modify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            orderId: order.orderId,
            permId: order.permId,
            ...requestBody,
            replaceOrder: { ...request.replaceOrder, client_attempt_id: attemptId },
            outsideRth,
            modifySequence: nextSeq,
          }),
        });
      } catch {
        return uncertain(); // dispatched or not, we cannot tell
      }
      let json: Record<string, unknown> | null = null;
      try {
        const parsed: unknown = await res.json();
        json = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
      } catch {
        json = null;
      }
      // The replace response carries no orders: refresh the panel once.
      void fetch("/api/orders", { method: "POST", headers: { "Content-Type": "application/json" } })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => data && ordersUpdaterRef.current?.(data as OrdersData))
        .catch(() => undefined);
      if (res.ok && json) {
        replaceAttemptsRef.current.delete(key);
        pushNotification({ type: "success", message: `${order.symbol} order replaced` });
        return { ok: true };
      }
      // An unreadable 200, an uncertain reason_code or an unexplained 5xx:
      // the replacement may be live at IB.
      if (res.ok || isUncertainPlaceResponse(res.status, json)) return uncertain();
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
      if (request.replaceOrder) return requestReplace(order, request);
      try {
        const { outsideRth, ...requestBody } = request;
        const nextSeq = (modifySeqCountsRef.current.get(order.permId) ?? 0) + 1;
        modifySeqCountsRef.current.set(order.permId, nextSeq);
        const res = await fetch("/api/orders/modify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            orderId: order.orderId,
            permId: order.permId,
            ...requestBody,
            outsideRth,
            modifySequence: nextSeq,
          }),
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok) {
          const reasonCode = readReasonCode(json);
          // On MODIFY_STALE the server echoes the current sequence under
          // detail.applied — sync our local counter so the next attempt is
          // (applied + 1) instead of fighting the server.
          if (reasonCode === "MODIFY_STALE") {
            const detail = (json as { detail?: unknown })?.detail;
            const applied =
              detail && typeof detail === "object"
                ? (detail as Record<string, unknown>).applied
                : undefined;
            if (typeof applied === "number") {
              modifySeqCountsRef.current.set(order.permId, applied);
            }
          }
          const message = reasonCode
            ? getReasonToast(reasonCode).copy
            : json?.error ||
              (typeof json?.detail === "string"
                ? json.detail
                : json?.detail?.message) ||
              "Modify failed";
          pushNotification({ type: "error", message });
          return { ok: false, status: res.status, reasonCode, message };
        }
        // Server may echo the applied sequence on success; if so, anchor
        // the local counter to it so subsequent attempts stay monotonic.
        const appliedSeq = (json as { applied_sequence?: unknown })
          ?.applied_sequence;
        if (typeof appliedSeq === "number") {
          modifySeqCountsRef.current.set(order.permId, appliedSeq);
        }
        if (request.newPrice != null || request.newQuantity != null) {
          const pm: PendingModify = {
            order,
            newPrice: request.newPrice,
            newQuantity: request.newQuantity,
          };
          setPendingModifies((prev) => new Map(prev).set(order.permId, pm));
        }

        if (json.orders) {
          pushOrdersData(json.orders);
        }

        if (request.newPrice != null || request.newQuantity != null) {
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
    [pushNotification, startModifyPoll, pushOrdersData, requestReplace],
  );

  /* ── Shared infrastructure ──────────────────────────── */

  const drainNotifications = useCallback((): Notification[] => {
    if (notificationsRef.current.length === 0) return [];
    const batch = notificationsRef.current;
    notificationsRef.current = [];
    return batch;
  }, []);

  const setOrdersUpdater = useCallback(
    (fn: ((data: OrdersData) => void) | null) => {
      ordersUpdaterRef.current = fn;
    },
    [],
  );

  // Cleanup all poll timers on unmount (read map at teardown so late pollers are cleared)
  useEffect(() => {
    return () => {
      // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional ref read at unmount
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
        requestCancel,
        requestModify,
        drainNotifications,
        setOrdersUpdater,
        lockedReplacements,
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
