import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { placeOutcomeUnknownBody } from "@/lib/order/placeOutcome";
import { firstPlaceOrderSchemaErrorMessage } from "@/lib/placeOrderBodySchema";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { getRequestId } from "@/lib/apiContracts";
import type { ModifyCancelTarget, ReplaceComboOrder } from "@/lib/orderModify";
import type { OrdersData } from "@/lib/order/types";
import { forwardRequestQuery } from "@/lib/forwardRequestQuery";

export const runtime = "nodejs";

type ModifyBody = {
  orderId?: number;
  permId?: number;
  brokerOrderId?: string;
  newPrice?: number;
  newQuantity?: number;
  outsideRth?: boolean;
  cancelOrders?: ModifyCancelTarget[];
  replaceOrder?: ReplaceComboOrder;
  modifySequence?: number;
};

function findOpenOrder(orders: OrdersData, orderId: number, permId: number) {
  return orders.open_orders.find(
    (order) =>
      (permId > 0 && order.permId === permId) ||
      (orderId > 0 && order.orderId === orderId),
  );
}

function isModifyConfirmed(
  orders: OrdersData,
  orderId: number,
  permId: number,
  newPrice?: number,
  newQuantity?: number,
): boolean {
  const order = findOpenOrder(orders, orderId, permId);
  if (!order) return false;

  const priceConfirmed =
    newPrice == null ||
    (order.limitPrice != null && Math.abs(order.limitPrice - newPrice) < 0.001);
  const quantityConfirmed =
    newQuantity == null || order.totalQuantity === newQuantity;

  return priceConfirmed && quantityConfirmed;
}

async function fetchOrders(scopeQs: string): Promise<OrdersData | null> {
  try {
    return await xenonFetch<OrdersData>(`/orders${scopeQs}`, {
      method: "GET",
      timeout: 10_000,
    });
  } catch {
    return null;
  }
}

export async function POST(request: Request): Promise<Response> {
  const scopeQs = forwardRequestQuery(request);
  const requestId = getRequestId();
  try {
    const body = (await request.json()) as ModifyBody;
    const orderId = body.orderId ?? 0;
    const permId = body.permId ?? 0;
    const brokerOrderId = body.brokerOrderId?.trim() ?? "";
    const newPrice = body.newPrice;
    const newQuantity = body.newQuantity;
    const replaceOrder = body.replaceOrder;

    if (orderId === 0 && permId === 0 && !brokerOrderId) {
      return NextResponse.json(
        {
          error: "Must provide orderId, permId, or brokerOrderId",
          reason_code: "ORDER_IDENTIFIER_REQUIRED",
        },
        { status: 400 },
      );
    }

    if (replaceOrder) {
      // Validation. Note: combo limitPrice may be negative (credit spread closes,
      // credit-spread modifies). The route accepts any non-zero finite number.
      const limitPriceValid =
        replaceOrder.limitPrice != null &&
        Number.isFinite(replaceOrder.limitPrice) &&
        replaceOrder.limitPrice !== 0;
      if (
        replaceOrder.type !== "combo" ||
        !replaceOrder.symbol ||
        !replaceOrder.action ||
        !replaceOrder.quantity ||
        !limitPriceValid ||
        !replaceOrder.legs ||
        replaceOrder.legs.length < 2
      ) {
        return NextResponse.json(
          {
            error: "Invalid combo replacement payload",
            reason_code: "INVALID_ORDER_BODY",
          },
          { status: 400 },
        );
      }
      // The replacement must pass the same body schema /orders/place
      // enforces (incl. client_attempt_id, the idempotency key) BEFORE the
      // original is cancelled — otherwise FastAPI rejects it after the
      // cancel and the working order is lost with nothing in its place.
      const replaceSchemaErr = firstPlaceOrderSchemaErrorMessage(replaceOrder);
      if (replaceSchemaErr) {
        return NextResponse.json(
          {
            error: `Invalid combo replacement payload: ${replaceSchemaErr}`,
            reason_code: "INVALID_ORDER_BODY",
          },
          { status: 400 },
        );
      }

      // FastAPI owns claim → cancel → place in one process (PR-07 fix3):
      // a second replacement of the same order is refused there before any
      // cancel. Forward body and status unchanged; only a failure that
      // proves nothing (timeout, socket reset, uncoded 5xx) becomes the
      // uncertain 502 — the original may be cancelled and the successor live.
      try {
        const result = await xenonFetch<Record<string, unknown>>(
          `/orders/replace${scopeQs}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              orderId,
              permId,
              cancelOrders: body.cancelOrders,
              replaceOrder,
            }),
            timeout: 120_000,
          },
        );
        return NextResponse.json(result);
      } catch (replaceErr) {
        const uncertainBody = placeOutcomeUnknownBody(replaceErr);
        if (uncertainBody) {
          return NextResponse.json(uncertainBody, { status: 502 });
        }
        return passThroughXenonError(replaceErr, requestId);
      }
    }

    if (newPrice == null && newQuantity == null && body.outsideRth == null) {
      return NextResponse.json(
        {
          error:
            "Must provide at least one modify field: newPrice, newQuantity, or outsideRth",
          reason_code: "INVALID_ORDER_BODY",
        },
        { status: 400 },
      );
    }

    // Credit combos carry a NEGATIVE BAG limit. The modify request has no
    // combo flag, so negatives pass through — the CLI knows the located
    // trade's secType and decides. Only 0 is an unconditional local reject.
    if (newPrice != null && newPrice === 0) {
      return NextResponse.json(
        {
          error: "Must provide newPrice != 0",
          reason_code: "INVALID_ORDER_BODY",
        },
        { status: 400 },
      );
    }

    if (newQuantity != null && newQuantity <= 0) {
      return NextResponse.json(
        {
          error: "Must provide newQuantity > 0",
          reason_code: "INVALID_ORDER_BODY",
        },
        { status: 400 },
      );
    }

    const modifyPayload = brokerOrderId
      ? {
          brokerOrderId,
          newPrice,
          newQuantity,
          modifySequence: body.modifySequence,
        }
      : {
          orderId,
          permId,
          newPrice,
          newQuantity,
          outsideRth: body.outsideRth,
          modifySequence: body.modifySequence,
        };

    const result = await xenonFetch<Record<string, unknown>>(`/orders/modify${scopeQs}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(modifyPayload),
      timeout: 20_000,
    });

    // Refresh orders after modify
    try {
      await xenonFetch(`/orders/refresh${scopeQs}`, { method: "POST", timeout: 10_000 });
    } catch {
      // Non-fatal
    }
    const orders = await fetchOrders(scopeQs);

    if (!orders) {
      return NextResponse.json(
        { error: "Modify completed but refreshed orders were unavailable" },
        { status: 502 },
      );
    }

    const futuScope = scopeQs.includes("broker=FUTU");
    if (
      !futuScope &&
      !isModifyConfirmed(orders, orderId, permId, newPrice, newQuantity)
    ) {
      return NextResponse.json(
        {
          error: "Modify not confirmed by refreshed orders",
          orders,
        },
        { status: 502 },
      );
    }

    return NextResponse.json({
      status: "ok",
      message: result.message,
      orders,
      ...(typeof result.confirmed === "boolean"
        ? { confirmed: result.confirmed }
        : {}),
      ...(typeof result.applied_sequence === "number"
        ? { applied_sequence: result.applied_sequence }
        : {}),
    });
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
