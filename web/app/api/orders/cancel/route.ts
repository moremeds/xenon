import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { getRequestId } from "@/lib/apiContracts";
import { forwardRequestQuery } from "@/lib/forwardRequestQuery";

export const runtime = "nodejs";

type CancelBody = {
  orderId?: number;
  permId?: number;
  brokerOrderId?: string;
};

export async function POST(request: Request): Promise<Response> {
  const scopeQs = forwardRequestQuery(request);
  const requestId = getRequestId();
  try {
    const body = (await request.json()) as CancelBody;
    const orderId = body.orderId ?? 0;
    const permId = body.permId ?? 0;
    const brokerOrderId = body.brokerOrderId?.trim() ?? "";

    if (orderId === 0 && permId === 0 && !brokerOrderId) {
      return NextResponse.json(
        {
          error: "Must provide orderId, permId, or brokerOrderId",
          reason_code: "ORDER_IDENTIFIER_REQUIRED",
        },
        { status: 400 },
      );
    }

    const cancelPayload = brokerOrderId
      ? { brokerOrderId }
      : { orderId, permId };

    const result = await xenonFetch<Record<string, unknown>>(`/orders/cancel${scopeQs}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cancelPayload),
      timeout: 20_000,
    });

    // Refresh orders after cancel
    try {
      await xenonFetch(`/orders/refresh${scopeQs}`, { method: "POST", timeout: 10_000 });
    } catch {
      // Non-fatal
    }
    let orders: unknown = null;
    try {
      orders = await xenonFetch(`/orders${scopeQs}`, { method: "GET", timeout: 10_000 });
    } catch {
      // Non-fatal; cancel success should still be returned.
    }

    return NextResponse.json({
      status: "ok",
      message: result.message,
      orders,
      ...(typeof result.confirmed === "boolean"
        ? { confirmed: result.confirmed }
        : {}),
      ...(typeof result.state === "string" ? { state: result.state } : {}),
      ...(typeof result.brokerOrderId === "string"
        ? { brokerOrderId: result.brokerOrderId }
        : {}),
    });
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
