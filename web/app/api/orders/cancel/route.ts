import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import { getRequestId } from "@/lib/apiContracts";

export const runtime = "nodejs";

type CancelBody = {
  orderId?: number;
  permId?: number;
};

export async function POST(request: Request): Promise<Response> {
  const requestId = getRequestId();
  try {
    const body = (await request.json()) as CancelBody;
    const orderId = body.orderId ?? 0;
    const permId = body.permId ?? 0;

    if (orderId === 0 && permId === 0) {
      return NextResponse.json(
        { error: "Must provide orderId or permId" },
        { status: 400 },
      );
    }

    const result = await xenonFetch<Record<string, unknown>>("/orders/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orderId, permId }),
      timeout: 20_000,
    });

    // Refresh orders after cancel
    try {
      await xenonFetch("/orders/refresh", { method: "POST", timeout: 10_000 });
    } catch {
      // Non-fatal
    }
    let orders: unknown = null;
    try {
      orders = await xenonFetch("/orders", { method: "GET", timeout: 10_000 });
    } catch {
      // Non-fatal; cancel success should still be returned.
    }

    return NextResponse.json({
      status: "ok",
      message: result.message,
      orders,
    });
  } catch (error) {
    return passThroughXenonError(error, requestId);
  }
}
