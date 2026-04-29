import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

type OrdersPayload = Record<string, unknown>;

let syncInFlight: Promise<void> | null = null;

async function fetchOrders(): Promise<OrdersPayload> {
  return await xenonFetch<OrdersPayload>("/orders", { method: "GET", timeout: 10_000 });
}

export async function GET(): Promise<Response> {
  try {
    const data = await fetchOrders();
    return NextResponse.json(data);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Failed to read orders";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}

export async function POST(): Promise<Response> {
  try {
    if (!syncInFlight) {
      syncInFlight = xenonFetch("/orders/refresh", { method: "POST", timeout: 35_000 })
        .then(() => {})
        .finally(() => {
          syncInFlight = null;
        });
    }
    await syncInFlight;

    const data = await fetchOrders();
    return NextResponse.json(data);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Sync failed";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
