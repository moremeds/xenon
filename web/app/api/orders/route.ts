import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

type OrdersPayload = Record<string, unknown>;

// Per-broker singleflight so an IB refresh and a FUTU refresh don't collapse together.
const syncInFlight: Record<string, Promise<void> | null> = {};

function brokerQuery(req?: Request): string {
  // `req` (or req.url) is absent when the route handler is unit-tested directly;
  // production Next.js always supplies a Request. Default to the IB scope.
  if (!req?.url) return "";
  const broker = new URL(req.url).searchParams.get("broker");
  return broker ? `?broker=${encodeURIComponent(broker)}` : "";
}

async function fetchOrders(qs: string): Promise<OrdersPayload> {
  return await xenonFetch<OrdersPayload>(`/orders${qs}`, {
    method: "GET",
    timeout: 10_000,
  });
}

export async function GET(req?: Request): Promise<Response> {
  const qs = brokerQuery(req);
  try {
    return NextResponse.json(await fetchOrders(qs));
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Failed to read orders";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}

export async function POST(req?: Request): Promise<Response> {
  const qs = brokerQuery(req);
  const key = qs || "ib";
  try {
    if (!syncInFlight[key]) {
      syncInFlight[key] = xenonFetch(`/orders/refresh${qs}`, {
        method: "POST",
        timeout: 35_000,
      })
        .then(() => {})
        .finally(() => {
          syncInFlight[key] = null;
        });
    }
    await syncInFlight[key];

    return NextResponse.json(await fetchOrders(qs));
  } catch (error) {
    const message = error instanceof Error ? error.message : "Sync failed";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
